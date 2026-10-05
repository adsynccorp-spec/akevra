import re
from datetime import timedelta

import pyotp
from django.core import mail
from django.test import TestCase, override_settings
from django.utils import timezone
from rest_framework.test import APIClient

from apps.accounts.models import Identity, Invitation, InvitationStatus, UserAccount
from apps.core import rls
from apps.rbac.models import OrganizationRoleGrant

PASSWORD = "Sprint0!Akevra"
NEW_PASSWORD = "Str0ng!Passw0rd"


@override_settings(AUTH_LOCKOUT_THRESHOLD=3)
class InvitationActivationTests(TestCase):
    """Invite-based account activation (RBAC-002: Administrator creates User Accounts)."""

    @classmethod
    def setUpTestData(cls):
        from django.core.management import call_command

        call_command("seed_sprint0", verbosity=0)

    def setUp(self):
        self.client = APIClient()

    # -- helpers -----------------------------------------------------------
    def login_as(self, email):
        client = APIClient()
        response = client.post("/api/v1/auth/login", {"email": email, "password": PASSWORD}, format="json")
        client.credentials(HTTP_AUTHORIZATION=f"Bearer {response.data['token']}")
        if response.data["status"] == "workspace_required":
            org = next(o for o in response.data["organizations"] if o["name"] == "Northshore ABA")
            client.post("/api/v1/auth/workspace/select", {"organization_id": org["id"]}, format="json")
        return client

    def invite(self, email="new.person@akevra.test", **extra):
        admin = self.login_as("settings.admin@akevra.test")
        response = admin.post("/api/v1/users/invitations", {"email": email, **extra}, format="json")
        self.assertEqual(response.status_code, 201, response.data)
        token = response.data["activation_url"].rsplit("/", 1)[1]
        return token, response.data

    def activate(self, token, name="Nia Brooks", password=NEW_PASSWORD):
        setup = self.client.post(
            f"/api/v1/auth/invitations/{token}/verify",
            {"full_name": name, "password": password},
            format="json",
        )
        self.assertEqual(setup.status_code, 200, setup.data)
        secret = setup.data["mfa_setup"]["secret"]
        response = self.client.post(
            f"/api/v1/auth/invitations/{token}/activate",
            {"full_name": name, "password": password, "code": pyotp.TOTP(secret).now()},
            format="json",
        )
        return response, secret

    # -- administrator side ------------------------------------------------
    def test_only_administrator_can_invite(self):
        supervisor = self.login_as("jordan@akevra.test")
        response = supervisor.post("/api/v1/users/invitations", {"email": "x@akevra.test"}, format="json")
        self.assertEqual(response.status_code, 403)

    def test_invitation_emails_link_and_hides_token(self):
        token, data = self.invite(credential_type="rbt")
        self.assertEqual(data["role_label"], "RBT")
        self.assertEqual(len(mail.outbox), 1)
        self.assertIn(f"/activate/{token}", mail.outbox[0].body)
        with rls.rls_bypass():
            self.assertNotEqual(Invitation.objects.get(email="new.person@akevra.test").token_hash, token)

    def test_cannot_invite_existing_member(self):
        admin = self.login_as("settings.admin@akevra.test")
        response = admin.post("/api/v1/users/invitations", {"email": "alex@akevra.test"}, format="json")
        self.assertEqual(response.status_code, 400)

    def test_reinvite_supersedes_previous_link(self):
        old_token, _ = self.invite()
        self.invite()
        self.assertEqual(self.client.get(f"/api/v1/auth/invitations/{old_token}").status_code, 410)

    # -- invitee side --------------------------------------------------------
    def test_full_activation_creates_account_with_mfa_and_signs_in(self):
        token, _ = self.invite(credential_type="rbt", org_role="clinical_director")

        details = self.client.get(f"/api/v1/auth/invitations/{token}")
        self.assertEqual(details.status_code, 200)
        self.assertEqual(details.data["organization"]["name"], "Northshore ABA")
        self.assertEqual(details.data["role_label"], "Clinical Director")
        self.assertFalse(details.data["existing_account"])

        response, secret = self.activate(token)
        self.assertEqual(response.status_code, 201, response.data)
        self.assertEqual(response.data["status"], "authenticated")
        self.assertEqual(response.data["user"]["full_name"], "Nia Brooks")
        self.assertEqual(response.data["user"]["org_roles"], ["clinical_director"])

        with rls.rls_bypass():
            identity = Identity.objects.get(email="new.person@akevra.test")
            self.assertTrue(identity.mfa_enabled)
            self.assertTrue(OrganizationRoleGrant.objects.filter(user_account__identity=identity).exists())
            self.assertEqual(Invitation.objects.get(email=identity.email).status, InvitationStatus.ACCEPTED)

        # The link is single-use, and the new login now requires the authenticator
        self.assertEqual(self.client.get(f"/api/v1/auth/invitations/{token}").status_code, 410)
        login = self.client.post(
            "/api/v1/auth/login", {"email": "new.person@akevra.test", "password": NEW_PASSWORD}, format="json"
        )
        self.assertEqual(login.data["status"], "mfa_required")

    def test_weak_password_rejected(self):
        token, _ = self.invite()
        response = self.client.post(
            f"/api/v1/auth/invitations/{token}/verify",
            {"full_name": "Nia Brooks", "password": "password"},
            format="json",
        )
        self.assertEqual(response.status_code, 400)
        self.assertIn("uppercase", response.data["detail"])

    def test_wrong_codes_burn_the_invitation(self):
        token, _ = self.invite()
        self.client.post(
            f"/api/v1/auth/invitations/{token}/verify",
            {"full_name": "Nia Brooks", "password": NEW_PASSWORD},
            format="json",
        )
        payload = {"full_name": "Nia Brooks", "password": NEW_PASSWORD, "code": "000000"}
        url = f"/api/v1/auth/invitations/{token}/activate"
        self.assertEqual(self.client.post(url, payload, format="json").status_code, 400)
        self.assertEqual(self.client.post(url, payload, format="json").status_code, 400)
        self.assertEqual(self.client.post(url, payload, format="json").status_code, 410)
        with rls.rls_bypass():
            self.assertFalse(UserAccount.objects.filter(email="new.person@akevra.test").exists())

    def test_expired_invitation_is_gone_with_details(self):
        token, _ = self.invite()
        with rls.rls_bypass():
            Invitation.objects.filter(email="new.person@akevra.test").update(
                expires_at=timezone.now() - timedelta(minutes=1)
            )
        response = self.client.get(f"/api/v1/auth/invitations/{token}")
        self.assertEqual(response.status_code, 410)
        self.assertEqual(response.data["invitation"]["state"], "expired")

    def test_revoked_invitation_cannot_activate(self):
        token, data = self.invite()
        admin = self.login_as("settings.admin@akevra.test")
        self.assertEqual(admin.post(f"/api/v1/users/invitations/{data['id']}/revoke").status_code, 200)
        response = self.client.post(
            f"/api/v1/auth/invitations/{token}/verify",
            {"full_name": "Nia Brooks", "password": NEW_PASSWORD},
            format="json",
        )
        self.assertEqual(response.status_code, 410)

    def test_unknown_token_is_not_found(self):
        self.assertEqual(self.client.get("/api/v1/auth/invitations/not-a-real-token").status_code, 404)

    def test_existing_login_joins_second_organization_with_own_password(self):
        # summit.rbt already has an AKEVRA login in Summit (DEC-062)
        token, _ = self.invite(email="summit.rbt@akevra.test", credential_type="rbt")
        self.assertTrue(self.client.get(f"/api/v1/auth/invitations/{token}").data["existing_account"])

        wrong = self.client.post(
            f"/api/v1/auth/invitations/{token}/verify",
            {"full_name": "Robin Patel", "password": NEW_PASSWORD},
            format="json",
        )
        self.assertEqual(wrong.status_code, 400)

        response, _ = self.activate(token, name="Robin Patel", password=PASSWORD)
        self.assertEqual(response.status_code, 201, response.data)
        self.assertEqual(response.data["status"], "workspace_required")
        self.assertEqual(len(response.data["organizations"]), 2)


@override_settings(AUTH_LOCKOUT_THRESHOLD=3)
class PasswordResetTests(TestCase):
    """Forgot password: emailed 6-digit code -> verify -> new password."""

    EMAIL = "alex@akevra.test"

    @classmethod
    def setUpTestData(cls):
        from django.core.management import call_command

        call_command("seed_sprint0", verbosity=0)

    def setUp(self):
        self.client = APIClient()

    def post(self, path, data):
        return self.client.post(f"/api/v1/auth/password/{path}", data, format="json")

    def request_code(self, email=EMAIL):
        mail.outbox.clear()
        self.assertEqual(self.post("forgot", {"email": email}).status_code, 200)
        return re.search(r"\b(\d{6})\b", mail.outbox[-1].body).group(1)

    def login(self, password):
        return self.client.post("/api/v1/auth/login", {"email": self.EMAIL, "password": password}, format="json")

    def test_unknown_email_gets_same_answer_and_no_email(self):
        known = self.post("forgot", {"email": self.EMAIL})
        mail.outbox.clear()
        unknown = self.post("forgot", {"email": "nobody@akevra.test"})
        self.assertEqual(unknown.status_code, 200)
        self.assertEqual(unknown.data, known.data)
        self.assertEqual(len(mail.outbox), 0)

    def test_full_reset_changes_password_and_signs_out_sessions(self):
        old_session = self.login(PASSWORD).data["token"]
        code = self.request_code()

        self.assertEqual(self.post("verify", {"email": self.EMAIL, "code": code}).status_code, 200)
        weak = self.post("reset", {"email": self.EMAIL, "code": code, "password": "weakpass"})
        self.assertEqual(weak.status_code, 400)
        self.assertIn("uppercase", weak.data["detail"])

        done = self.post("reset", {"email": self.EMAIL, "code": code, "password": NEW_PASSWORD})
        self.assertEqual(done.status_code, 200, done.data)
        self.assertEqual(self.login(PASSWORD).status_code, 401)
        self.assertEqual(self.login(NEW_PASSWORD).status_code, 200)
        self.assertIn("changed", mail.outbox[-1].subject)

        stale = APIClient()
        stale.credentials(HTTP_AUTHORIZATION=f"Bearer {old_session}")
        self.assertEqual(stale.get("/api/v1/auth/me").status_code, 401)

        # the code is single-use
        again = self.post("reset", {"email": self.EMAIL, "code": code, "password": "An0ther!Pass"})
        self.assertEqual(again.status_code, 400)

    def test_wrong_codes_burn_the_code(self):
        code = self.request_code()
        wrong = "000000" if code != "000000" else "111111"
        for _ in range(2):
            self.assertEqual(self.post("verify", {"email": self.EMAIL, "code": wrong}).status_code, 400)
        burned = self.post("verify", {"email": self.EMAIL, "code": wrong})
        self.assertIn("Too many", burned.data["detail"])
        self.assertEqual(self.post("verify", {"email": self.EMAIL, "code": code}).status_code, 400)

    def test_expired_code_rejected(self):
        code = self.request_code()
        with rls.rls_bypass():
            Identity.objects.filter(email=self.EMAIL).update(password_reset_expires_at=timezone.now() - timedelta(seconds=1))
        self.assertEqual(self.post("verify", {"email": self.EMAIL, "code": code}).status_code, 400)

    def test_resend_within_cooldown_keeps_first_code(self):
        code = self.request_code()
        mail.outbox.clear()
        self.post("forgot", {"email": self.EMAIL})
        self.assertEqual(len(mail.outbox), 0)
        self.assertEqual(self.post("verify", {"email": self.EMAIL, "code": code}).status_code, 200)

    def test_reset_unlocks_a_locked_account(self):
        with rls.rls_bypass():
            Identity.objects.filter(email=self.EMAIL).update(locked_until=timezone.now() + timedelta(minutes=15))
        self.assertEqual(self.login(PASSWORD).status_code, 423)
        code = self.request_code()
        self.post("reset", {"email": self.EMAIL, "code": code, "password": NEW_PASSWORD})
        self.assertEqual(self.login(NEW_PASSWORD).status_code, 200)


class EmailDeliveryReportTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        from django.core.management import call_command

        call_command("seed_sprint0", verbosity=0)

    def invite(self):
        client = APIClient()
        login = client.post("/api/v1/auth/login", {"email": "settings.admin@akevra.test", "password": PASSWORD}, format="json")
        client.credentials(HTTP_AUTHORIZATION=f"Bearer {login.data['token']}")
        return client.post("/api/v1/users/invitations", {"email": "report@akevra.test"}, format="json").data

    def test_delivered_email_is_reported(self):
        data = self.invite()
        self.assertTrue(data["email_sent"])
        self.assertIsNone(data["email_error"])

    @override_settings(EMAIL_BACKEND="django.core.mail.backends.console.EmailBackend")
    def test_unconfigured_email_is_reported_with_reason(self):
        data = self.invite()
        self.assertFalse(data["email_sent"])
        self.assertIn("not configured", data["email_error"])
