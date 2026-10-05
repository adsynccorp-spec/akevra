import time
from datetime import date, timedelta

import pyotp
from django.conf import settings
from django.db import connection
from django.test import TestCase, override_settings
from django.utils import timezone
from rest_framework.test import APIClient

from apps.accounts.models import AuthSession, Identity, UserAccount
from apps.audit.models import AuditEvent
from apps.core.exceptions import HardDeleteNotAllowed
from apps.core.management.commands.seed_sprint0 import MFA_SECRET
from apps.core.models import RecordStatus
from apps.organizations.models import Organization
from apps.rbac.matrix import MATRIX, PERMISSIONS
from apps.rbac.models import OrgRole, RelationshipRole
from apps.rbac.services import has_permission
from apps.supervision.models import SupervisoryRelationship

PASSWORD = "Sprint0!Akevra"


class Sprint0AcceptanceTests(TestCase):
    """Maps 1:1 to the ten Sprint 0 milestone acceptance criteria."""

    @classmethod
    def setUpTestData(cls):
        from django.core.management import call_command

        call_command("seed_sprint0", verbosity=0)

    def setUp(self):
        self.client = APIClient()

    def auth(self, email, organization_name=None, password=PASSWORD):
        response = self.client.post(
            "/api/v1/auth/login",
            {"email": email, "password": password},
            format="json",
        )
        token = response.data.get("token")
        self.assertIsNotNone(token, response.data)
        self.client.credentials(HTTP_AUTHORIZATION=f"Bearer {token}")
        if response.data.get("status") == "mfa_required":
            code = pyotp.TOTP(MFA_SECRET).now()
            response = self.client.post("/api/v1/auth/mfa/verify", {"code": code}, format="json")
            if response.data.get("token"):
                self.client.credentials(HTTP_AUTHORIZATION=f"Bearer {response.data['token']}")
        if response.data.get("status") == "workspace_required":
            orgs = response.data["organizations"]
            if organization_name:
                chosen = next(o for o in orgs if o["name"] == organization_name)
            else:
                chosen = orgs[0]
            response = self.client.post(
                "/api/v1/auth/workspace/select",
                {"organization_id": chosen["id"]},
                format="json",
            )
        self.assertEqual(response.data.get("status"), "authenticated", response.data)
        return response

    def test_01_schema_and_user_uniqueness(self):
        tables = {
            "organization",
            "login_identity",
            "user_account",
            "supervisory_relationship",
            "audit_event",
            "development_plan",
            "monthly_cycle",
            "supervision_session",
            "compliance_rule_set",
        }
        with connection.cursor() as cursor:
            cursor.execute(
                "SELECT tablename FROM pg_tables WHERE schemaname='public'"
            )
            present = {row[0] for row in cursor.fetchall()}
        self.assertTrue(tables.issubset(present), tables - present)

        admin = Identity.objects.get(email="admin@akevra.test")
        self.assertEqual(admin.user_accounts.count(), 2)
        with self.assertRaises(Exception):
            UserAccount.objects.create(
                identity=admin,
                organization=admin.user_accounts.first().organization,
                email=admin.email,
                first_name="Dup",
                last_name="User",
            )

    def test_02_cross_organization_denied_at_db_and_api(self):
        northshore = Organization.objects.get(slug="northshore-aba")
        summit = Organization.objects.get(slug="summit-behavioral")
        foreign = SupervisoryRelationship.objects.get(organization=summit)

        with connection.cursor() as cursor:
            cursor.execute("SET ROLE akevra_app")
            cursor.execute("SELECT set_config('app.rls_bypass', 'off', false)")
            cursor.execute(
                "SELECT set_config('app.current_organization_id', %s, false)",
                [str(northshore.id)],
            )
            cursor.execute(
                "SELECT count(*) FROM supervisory_relationship WHERE id = %s",
                [str(foreign.id)],
            )
            hidden = cursor.fetchone()[0]
            cursor.execute("RESET ROLE")
        self.assertEqual(hidden, 0)

        self.auth("jordan@akevra.test")
        response = self.client.get(f"/api/v1/relationships/{foreign.id}")
        self.assertIn(response.status_code, (403, 404))
        listing = self.client.get("/api/v1/relationships")
        ids = {row["id"] for row in listing.data}
        self.assertNotIn(str(foreign.id), ids)

    def test_03_login_mfa_lockout_and_session_timeout(self):
        response = self.client.post(
            "/api/v1/auth/login",
            {"email": "mfa@akevra.test", "password": PASSWORD},
            format="json",
        )
        self.assertEqual(response.data["status"], "mfa_required")
        self.client.credentials(HTTP_AUTHORIZATION=f"Bearer {response.data['token']}")
        bad = self.client.post("/api/v1/auth/mfa/verify", {"code": "000000"}, format="json")
        self.assertEqual(bad.status_code, 401)
        good = self.client.post(
            "/api/v1/auth/mfa/verify",
            {"code": pyotp.TOTP(MFA_SECRET).now()},
            format="json",
        )
        self.assertEqual(good.data["status"], "authenticated")

        for _ in range(settings.AUTH_LOCKOUT_THRESHOLD):
            locked = self.client.post(
                "/api/v1/auth/login",
                {"email": "lockout@akevra.test", "password": "wrong"},
                format="json",
            )
        self.assertIn(locked.status_code, (401, 423))
        still = self.client.post(
            "/api/v1/auth/login",
            {"email": "lockout@akevra.test", "password": PASSWORD},
            format="json",
        )
        self.assertEqual(still.status_code, 423)

        with override_settings(AUTH_SESSION_IDLE_SECONDS=1):
            self.client = APIClient()
            self.auth("alex@akevra.test")
            session = AuthSession.objects.order_by("-created_at").first()
            session.last_seen_at = session.last_seen_at - timedelta(seconds=5)
            session.save(update_fields=["last_seen_at"])
            expired = self.client.get("/api/v1/auth/me")
            self.assertEqual(expired.status_code, 401)

    def test_04_rbac_matrix_for_every_approved_role(self):
        for role in (OrgRole.ADMINISTRATOR, OrgRole.CLINICAL_DIRECTOR,
                     RelationshipRole.SUPERVISOR, RelationshipRole.SUPERVISEE):
            self.assertEqual(set(MATRIX[role]), set(PERMISSIONS))

        self.auth("director@akevra.test")
        matrix = self.client.get("/api/v1/rbac/matrix")
        self.assertEqual(matrix.status_code, 200)
        self.assertIn("supervisor", matrix.data["roles"])

        jordan = UserAccount.objects.get(email="jordan@akevra.test")
        as_supervisor = SupervisoryRelationship.objects.get(supervisor=jordan)
        as_supervisee = SupervisoryRelationship.objects.get(supervisee=jordan)
        self.assertTrue(has_permission(jordan, "session.document", as_supervisor))
        self.assertFalse(has_permission(jordan, "session.document", as_supervisee))

        self.auth("alex@akevra.test")
        denied = self.client.post(
            "/api/v1/relationships",
            {
                "supervisor_id": str(jordan.id),
                "supervisee_id": str(UserAccount.objects.get(email="alex@akevra.test").id),
                "supervision_track": "rbt_ongoing_supervision",
                "started_on": "2026-08-01",
            },
            format="json",
        )
        self.assertEqual(denied.status_code, 403)

    def test_05_audit_history_and_no_hard_delete(self):
        before = AuditEvent.objects.count()
        self.auth("jordan@akevra.test")
        rel = SupervisoryRelationship.objects.get(supervisee__email="alex@akevra.test")
        response = self.client.patch(
            f"/api/v1/relationships/{rel.id}",
            {"status": "active"},
            format="json",
        )
        self.assertEqual(response.status_code, 200)
        self.assertGreater(AuditEvent.objects.count(), before)

        with self.assertRaises(HardDeleteNotAllowed):
            rel.delete()
        with self.assertRaises(HardDeleteNotAllowed):
            AuditEvent.objects.first().delete()

        delete = self.client.delete(f"/api/v1/relationships/{rel.id}")
        self.assertEqual(delete.status_code, 405)

        with connection.cursor() as cursor:
            with self.assertRaises(Exception):
                cursor.execute("DELETE FROM supervisory_relationship WHERE id = %s", [str(rel.id)])

    def test_08_workspace_selection_req_094(self):
        single = self.client.post(
            "/api/v1/auth/login",
            {"email": "alex@akevra.test", "password": PASSWORD},
            format="json",
        )
        self.assertEqual(single.data["status"], "authenticated")
        self.assertEqual(single.data["user"]["organization"]["slug"], "northshore-aba")

        multi = self.client.post(
            "/api/v1/auth/login",
            {"email": "admin@akevra.test", "password": PASSWORD},
            format="json",
        )
        self.assertEqual(multi.data["status"], "workspace_required")
        names = {o["name"] for o in multi.data["organizations"]}
        self.assertEqual(names, {"Northshore ABA", "Summit Behavioral"})

        self.client.credentials(HTTP_AUTHORIZATION=f"Bearer {multi.data['token']}")
        northshore = next(o for o in multi.data["organizations"] if o["name"] == "Northshore ABA")
        selected = self.client.post(
            "/api/v1/auth/workspace/select",
            {"organization_id": northshore["id"]},
            format="json",
        )
        self.assertEqual(selected.data["status"], "authenticated")
        listing = self.client.get("/api/v1/relationships")
        self.assertEqual(listing.status_code, 200)
        for row in listing.data:
            self.assertEqual(row["organization_id"], northshore["id"])

        summit = Organization.objects.get(slug="summit-behavioral")
        foreign = SupervisoryRelationship.objects.get(organization=summit)
        hidden = self.client.get(f"/api/v1/relationships/{foreign.id}")
        self.assertIn(hidden.status_code, (403, 404))

    def test_09_rbac_003_administrator_self_party(self):
        party = UserAccount.objects.get(email="party.admin@akevra.test")
        own = SupervisoryRelationship.objects.get(supervisee=party)
        other = SupervisoryRelationship.objects.get(supervisee__email="alex@akevra.test")

        self.auth("party.admin@akevra.test")
        allowed = self.client.patch(
            f"/api/v1/relationships/{own.id}",
            {"status": "active"},
            format="json",
        )
        self.assertEqual(allowed.status_code, 200)
        denied_other = self.client.patch(
            f"/api/v1/relationships/{other.id}",
            {"status": "active"},
            format="json",
        )
        self.assertEqual(denied_other.status_code, 403)
        clinical = self.client.get(
            f"/api/v1/rbac/evaluate?permission=session.document&relationship_id={own.id}"
        )
        self.assertFalse(clinical.data["allowed"])

        self.client = APIClient()
        self.auth("settings.admin@akevra.test")
        listing = self.client.get("/api/v1/relationships")
        self.assertEqual(listing.data, [])
        denied = self.client.patch(
            f"/api/v1/relationships/{own.id}",
            {"status": "active"},
            format="json",
        )
        self.assertIn(denied.status_code, (403, 404))

    def test_10_dec_057_relationship_scoped_roles(self):
        jordan = UserAccount.objects.get(email="jordan@akevra.test")
        as_supervisor = SupervisoryRelationship.objects.get(supervisor=jordan)
        as_supervisee = SupervisoryRelationship.objects.get(supervisee=jordan)

        self.auth("jordan@akevra.test")
        me = self.client.get("/api/v1/auth/me")
        roles = {row["role"] for row in me.data["relationships"]}
        self.assertEqual(roles, {"supervisor", "supervisee"})

        sup = self.client.get(
            f"/api/v1/rbac/evaluate?permission=session.document&relationship_id={as_supervisor.id}"
        )
        sub = self.client.get(
            f"/api/v1/rbac/evaluate?permission=session.document&relationship_id={as_supervisee.id}"
        )
        self.assertTrue(sup.data["allowed"])
        self.assertFalse(sub.data["allowed"])
        self.assertEqual(sup.data["relationship_role"], "supervisor")
        self.assertEqual(sub.data["relationship_role"], "supervisee")

        edit_as_supervisee = self.client.patch(
            f"/api/v1/relationships/{as_supervisee.id}",
            {"status": "active"},
            format="json",
        )
        self.assertEqual(edit_as_supervisee.status_code, 403)

    def test_11_mfa_pending_token_cannot_access_protected_endpoint(self):
        """An MFA-pending session token must be rejected at protected endpoints."""
        response = self.client.post(
            "/api/v1/auth/login",
            {"email": "mfa@akevra.test", "password": PASSWORD},
            format="json",
        )
        self.assertEqual(response.data["status"], "mfa_required")
        mfa_pending_token = response.data["token"]
        self.client.credentials(HTTP_AUTHORIZATION=f"Bearer {mfa_pending_token}")

        # Protected endpoint must reject the MFA-pending token
        protected = self.client.get("/api/v1/auth/me")
        self.assertIn(protected.status_code, (401, 403))

    def test_12_disabled_identity_and_archived_account_block_session(self):
        """Sessions are immediately blocked when identity or user_account is disabled."""
        from apps.core import rls

        # --- identity disabled ---
        self.auth("alex@akevra.test")
        identity = Identity.objects.get(email="alex@akevra.test")
        with rls.rls_bypass():
            identity.is_active = False
            identity.save(update_fields=["is_active"])

        blocked = self.client.get("/api/v1/auth/me")
        self.assertIn(blocked.status_code, (401, 403))

        # restore
        with rls.rls_bypass():
            identity.is_active = True
            identity.save(update_fields=["is_active"])

        # --- user_account.status archived ---
        self.client = APIClient()
        self.auth("alex@akevra.test")
        account = UserAccount.objects.get(email="alex@akevra.test")
        with rls.rls_bypass():
            account.status = RecordStatus.ARCHIVED
            account.save(update_fields=["status"])

        blocked2 = self.client.get("/api/v1/auth/me")
        self.assertIn(blocked2.status_code, (401, 403))

        # restore
        with rls.rls_bypass():
            account.status = RecordStatus.ACTIVE
            account.save(update_fields=["status"])

        # --- user_account.record_status archived (SoftArchiveModel field) ---
        self.client = APIClient()
        self.auth("alex@akevra.test")
        with rls.rls_bypass():
            account.record_status = RecordStatus.ARCHIVED
            account.save(update_fields=["record_status"])

        blocked3 = self.client.get("/api/v1/auth/me")
        self.assertIn(blocked3.status_code, (401, 403))

        # restore
        with rls.rls_bypass():
            account.record_status = RecordStatus.ACTIVE
            account.save(update_fields=["record_status"])

    def test_06_relationship_detail_authorized_retrieval(self):
        self.auth("jordan@akevra.test")
        jordan = UserAccount.objects.get(email="jordan@akevra.test")
        rel = SupervisoryRelationship.objects.get(supervisor=jordan)
        response = self.client.get(f"/api/v1/relationships/{rel.id}")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data["id"], str(rel.id))
        self.assertIn("your_role", response.data)
        self.assertEqual(response.data["your_role"], "supervisor")

    def test_07_walking_skeleton_landings(self):
        for email, view in (
            ("settings.admin@akevra.test", "administrator"),
            ("jordan@akevra.test", "supervisor"),
            ("alex@akevra.test", "supervisee"),
            ("director@akevra.test", "clinical_director"),
        ):
            self.client = APIClient()
            self.auth(email)
            me = self.client.get("/api/v1/auth/me")
            self.assertIn(view, me.data["landing_views"], email)


class StagingDataTests(TestCase):
    """QA Sandbox seed + replacing real personal data with synthetic demo data."""

    @classmethod
    def setUpTestData(cls):
        from django.core.management import call_command

        call_command("seed_sprint0", verbosity=0)

    def test_qa_sandbox_is_separate_and_seeded_once(self):
        from django.core.management import call_command

        sandbox = Organization.objects.get(slug="qa-sandbox")
        admin = UserAccount.objects.get(email="qa.admin@akevra.test")
        self.assertEqual(admin.organization, sandbox)
        self.assertEqual(admin.credential_type, "")
        self.assertEqual(SupervisoryRelationship.objects.filter(organization=sandbox).count(), 1)

        call_command("seed_sprint0", verbosity=0)  # safe to re-run
        self.assertEqual(Organization.objects.filter(slug="qa-sandbox").count(), 1)

    def test_scrub_replaces_real_emails_only_with_apply(self):
        from io import StringIO

        from django.core.management import call_command

        from apps.accounts.models import Invitation

        real = UserAccount.objects.get(email="jordan@akevra.test")
        Identity.objects.filter(pk=real.identity_id).update(email="someone.real@gmail.com")
        UserAccount.objects.filter(pk=real.pk).update(email="someone.real@gmail.com", first_name="Real", last_name="Person")
        Invitation.objects.create(
            organization=real.organization, email="invitee@outlook.com", token_hash="x" * 64,
            expires_at=timezone.now() + timedelta(hours=1), invited_by=real,
        )

        call_command("scrub_personal_data", stdout=StringIO())  # dry run
        self.assertTrue(Identity.objects.filter(email="someone.real@gmail.com").exists())

        call_command("scrub_personal_data", "--apply", stdout=StringIO())
        account = UserAccount.objects.get(pk=real.pk)
        self.assertTrue(account.email.endswith("@akevra.test"))
        self.assertEqual(account.first_name, "Demo")
        self.assertEqual(Identity.objects.get(pk=real.identity_id).email, account.email)
        invitation = Invitation.objects.get(token_hash="x" * 64)
        self.assertTrue(invitation.email.endswith("@akevra.test"))
        self.assertEqual(invitation.status, "revoked")
        self.assertFalse(Identity.objects.exclude(email__endswith="@akevra.test").exists())

        # the login still works with the same password under the new email
        response = APIClient().post("/api/v1/auth/login", {"email": account.email, "password": PASSWORD}, format="json")
        self.assertEqual(response.status_code, 200)
