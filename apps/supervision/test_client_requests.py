"""Client requests after M4/M5: BCBA consultation self-scheduling with available times and conflict
checks, the "Individual Consultation" label, biweekly "In Progress", BT development, the credential
rule for Supervised Fieldwork, workspace provisioning and switching, and two fixes."""

from datetime import date
from io import StringIO

from django.core.exceptions import ValidationError
from django.core.management import call_command
from django.db import connection
from django.test import TestCase
from rest_framework.test import APIClient

from apps.accounts.models import CredentialType, Invitation, UserAccount
from apps.core.tests import PASSWORD
from apps.organizations.models import Organization
from apps.supervision import periods
from apps.supervision.compliance import Rules, evaluate_ongoing_month
from apps.supervision.models import (
    AvailabilitySlot,
    Notification,
    SupervisionTrack,
    SupervisoryRelationship,
)


class ClientRequestTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        call_command("seed_sprint0", verbosity=0)
        cls.morgan = UserAccount.objects.get(email="morgan@akevra.test")  # BCBA Development Lead
        cls.jordan = UserAccount.objects.get(email="jordan@akevra.test")  # BCBA participant (and Alex's Supervisor)
        cls.alex = UserAccount.objects.get(email="alex@akevra.test")
        cls.pd = SupervisoryRelationship.objects.get(supervisor=cls.morgan, supervisee=cls.jordan)
        cls.rbt = SupervisoryRelationship.objects.get(supervisor=cls.jordan, supervisee=cls.alex)

    def as_user(self, email):
        client = APIClient()
        response = client.post("/api/v1/auth/login", {"email": email, "password": PASSWORD}, format="json")
        client.credentials(HTTP_AUTHORIZATION=f"Bearer {response.data['token']}")
        return client

    def book(self, client, rel, start, end, **extra):
        return client.post(f"/api/v1/relationships/{rel.id}/appointments",
                           {"starts_at": start, "ends_at": end, "session_type": "individual", "meeting_method": "remote", **extra},
                           format="json")

    # --- BCBA Professional Development: participant books, Lead is notified -------------------
    def test_bcba_participant_books_consultation_and_lead_is_notified(self):
        jordan = self.as_user("jordan@akevra.test")
        listing = jordan.get(f"/api/v1/relationships/{self.pd.id}/appointments").data
        self.assertTrue(listing["self_scheduling"])
        self.assertFalse(listing["can_manage"])

        booked = self.book(jordan, self.pd, "2026-12-01T15:00:00Z", "2026-12-01T16:00:00Z")
        self.assertEqual(booked.status_code, 201, booked.data)
        appointment = booked.data["appointments"][0]
        self.assertTrue(appointment["self_scheduled"])
        self.assertEqual(appointment["session_type_label"], "Individual Consultation")
        self.assertTrue(appointment["can_manage"])       # their own booking
        self.assertFalse(appointment["can_document"])    # documentation stays with the Lead
        note = Notification.objects.filter(recipient=self.morgan, kind="appointment_scheduled").latest("created_at")
        self.assertIn("booked a consultation", note.title)

        # The participant can move or cancel their own booking, never mark it held
        moved = jordan.patch(f"/api/v1/appointments/{appointment['id']}",
                             {"starts_at": "2026-12-01T17:00:00Z", "ends_at": "2026-12-01T18:00:00Z"}, format="json")
        self.assertEqual(moved.status_code, 200, moved.data)
        self.assertEqual(jordan.post(f"/api/v1/appointments/{appointment['id']}/complete").status_code, 403)
        self.assertEqual(jordan.post(f"/api/v1/appointments/{appointment['id']}/cancel", {"reason": "Clash"}, format="json").status_code, 200)

    def test_other_pathways_unchanged_supervisee_cannot_schedule(self):
        alex = self.as_user("alex@akevra.test")
        self.assertFalse(alex.get(f"/api/v1/relationships/{self.rbt.id}/appointments").data["self_scheduling"])
        self.assertEqual(self.book(alex, self.rbt, "2026-12-02T15:00:00Z", "2026-12-02T16:00:00Z").status_code, 403)
        # A Lead-scheduled RBT appointment keeps the supervision wording
        jordan = self.as_user("jordan@akevra.test")
        made = self.book(jordan, self.rbt, "2026-12-02T15:00:00Z", "2026-12-02T16:00:00Z").data["appointments"][0]
        self.assertEqual(made["session_type_label"], "Individual Supervision")

    def test_conflicting_times_are_refused_for_either_party(self):
        jordan, morgan = self.as_user("jordan@akevra.test"), self.as_user("morgan@akevra.test")
        self.assertEqual(self.book(jordan, self.rbt, "2026-12-03T15:00:00Z", "2026-12-03T16:00:00Z").status_code, 201)
        # Jordan is busy supervising Alex then, so a consultation overlapping it is refused
        clash = self.book(morgan, self.pd, "2026-12-03T15:30:00Z", "2026-12-03T16:30:00Z")
        self.assertEqual(clash.status_code, 400)
        self.assertIn("conflicts with another appointment for Jordan Lee", str(clash.data))
        # Back-to-back is fine
        self.assertEqual(self.book(morgan, self.pd, "2026-12-03T16:00:00Z", "2026-12-03T17:00:00Z").status_code, 201)
        # Rescheduling into a clash is refused too
        other = self.book(morgan, self.pd, "2026-12-04T16:00:00Z", "2026-12-04T17:00:00Z").data["appointments"][0]
        moved = morgan.patch(f"/api/v1/appointments/{other['id']}",
                             {"starts_at": "2026-12-03T16:30:00Z", "ends_at": "2026-12-03T17:30:00Z"}, format="json")
        self.assertEqual(moved.status_code, 400)

    def test_participant_books_only_inside_available_times(self):
        morgan, jordan = self.as_user("morgan@akevra.test"), self.as_user("jordan@akevra.test")
        # Tuesdays 9:00–12:00 New York time
        saved = morgan.put("/api/v1/availability", {"slots": [{"weekday": 1, "starts_at": "09:00", "ends_at": "12:00"}]}, format="json")
        self.assertEqual(saved.status_code, 200, saved.data)
        shown = jordan.get(f"/api/v1/relationships/{self.pd.id}/availability").data
        self.assertEqual([(s["weekday_label"], s["starts_at"], s["ends_at"]) for s in shown["slots"]], [("Tuesday", "09:00", "12:00")])

        # Dec 8 2026 is a Tuesday; 15:00Z = 10:00 EST (inside), 18:00Z = 13:00 EST (outside)
        inside = self.book(jordan, self.pd, "2026-12-08T15:00:00Z", "2026-12-08T16:00:00Z")
        self.assertEqual(inside.status_code, 201, inside.data)
        outside = self.book(jordan, self.pd, "2026-12-08T18:00:00Z", "2026-12-08T19:00:00Z")
        self.assertEqual(outside.status_code, 400)
        self.assertIn("available at these times only", str(outside.data))
        # The Lead isn't bound by their own published times
        self.assertEqual(self.book(morgan, self.pd, "2026-12-08T18:00:00Z", "2026-12-08T19:00:00Z").status_code, 201)
        # Overlapping windows are refused; replacing keeps the earlier set on file (archived)
        bad = morgan.put("/api/v1/availability", {"slots": [{"weekday": 1, "starts_at": "09:00", "ends_at": "12:00"},
                                                           {"weekday": 1, "starts_at": "11:00", "ends_at": "13:00"}]}, format="json")
        self.assertEqual(bad.status_code, 400)
        morgan.put("/api/v1/availability", {"slots": []}, format="json")
        self.assertEqual(AvailabilitySlot.objects.filter(owner=self.morgan, record_status="archived").count(), 1)

    def test_pd_sessions_are_labelled_consultation(self):
        morgan = self.as_user("morgan@akevra.test")
        session = morgan.post(f"/api/v1/relationships/{self.pd.id}/sessions",
                              {"occurred_on": "2026-10-20", "start_time": "10:00", "end_time": "11:00", "session_type": "individual",
                               "meeting_method": "in_person", "topics": "Case discussion"}, format="json")
        self.assertEqual(session.status_code, 201, session.data)
        self.assertEqual(session.data["session_type_label"], "Individual Consultation")

    # --- BCaBA biweekly: an interval still running is "In Progress", not "Below" ---------------
    def test_biweekly_interval_in_progress(self):
        rules = Rules({"min_supervision_percent": "5", "biweekly_min_minutes": "60"}, {})
        common = dict(service_hours=10, supervision_minutes=60, contacts=1, individual_contacts=1, observation_contacts=0)
        running = evaluate_ongoing_month(rules, first_half_minutes=60, second_half_minutes=0, open_halves=(1,), **common)
        check = next(c for c in running["checks"] if c["key"] == "biweekly_minutes")
        self.assertEqual((check["status"], check["met"]), ("in_progress", True))
        self.assertEqual([i["status"] for i in check["intervals"]], ["met", "in_progress"])
        ended = evaluate_ongoing_month(rules, first_half_minutes=60, second_half_minutes=0, **common)
        check = next(c for c in ended["checks"] if c["key"] == "biweekly_minutes")
        self.assertEqual((check["status"], check["met"]), ("not_met", False))
        missed_earlier = evaluate_ongoing_month(rules, first_half_minutes=0, second_half_minutes=0, open_halves=(1,), **common)
        self.assertEqual(next(c for c in missed_earlier["checks"] if c["key"] == "biweekly_minutes")["status"], "not_met")

    # --- BT development and the credential rule ----------------------------------------------
    def test_bt_development_and_no_credential_has_no_pathway(self):
        org = self.rbt.organization
        bt = UserAccount.objects.create(identity=self.alex.identity.__class__.objects.create_user(email="bt@akevra.test", password=PASSWORD),
                                        organization=org, email="bt@akevra.test", first_name="Bo", last_name="Tech",
                                        credential_type=CredentialType.BT)
        rel = SupervisoryRelationship.objects.create(organization=org, supervisor=self.jordan, supervisee=bt,
                                                     supervision_track=SupervisionTrack.BT_DEVELOPMENT, started_on=date(2026, 10, 1))
        self.assertFalse(rel.is_compliance_bearing)
        with self.assertRaises(ValidationError):  # a BT is never on BACB-regulated RBT supervision
            SupervisoryRelationship.objects.create(organization=org, supervisor=self.morgan, supervisee=bt,
                                                   supervision_track=SupervisionTrack.RBT_ONGOING, started_on=date(2026, 10, 1))
        jordan = self.as_user("jordan@akevra.test")
        self.assertEqual(jordan.get(f"/api/v1/relationships/{rel.id}/compliance").data["status"], "not_compliance_bearing")

        logan = UserAccount.objects.get(email="lockout@akevra.test")  # no credential on file
        with self.assertRaisesMessage(ValidationError, "no credential on their profile"):
            SupervisoryRelationship.objects.create(organization=org, supervisor=self.jordan, supervisee=logan,
                                                   supervision_track=SupervisionTrack.SUPERVISED_FIELDWORK,
                                                   fieldwork_subtype="supervised", started_on=date(2026, 10, 1))
        # An Administrator records the credential, after which the pathway opens
        admin = self.as_user("settings.admin@akevra.test")
        self.assertEqual(admin.post(f"/api/v1/users/members/{logan.id}/credential", {"credential_type": "student_analyst"}, format="json").status_code, 200)
        logan.refresh_from_db()
        SupervisoryRelationship.objects.create(organization=org, supervisor=self.jordan, supervisee=logan,
                                               supervision_track=SupervisionTrack.SUPERVISED_FIELDWORK,
                                               fieldwork_subtype="supervised", started_on=date(2026, 10, 1))
        self.assertEqual(self.as_user("alex@akevra.test").post(f"/api/v1/users/members/{logan.id}/credential",
                                                               {"credential_type": "rbt"}, format="json").status_code, 403)

    # --- Fix: confirming "No other agency" again ---------------------------------------------
    def test_no_other_agency_can_be_confirmed_again(self):
        first = periods.record_service_hours(self.rbt, self.alex, year=2026, month=3, hours="0", scope="external_none")
        again = periods.record_service_hours(self.rbt, self.alex, year=2026, month=3, hours="0", scope="external_none")
        self.assertEqual(first.pk, again.pk)

    # --- Workspaces: manual provisioning, switching, isolation -------------------------------
    def test_provision_individual_bcba_workspace(self):
        out = StringIO()
        call_command("provision_workspace", "--name", "Jordan Lee Supervision", "--admin-email", "jordan@akevra.test",
                     "--individual", stdout=out)
        org = Organization.objects.get(slug="jordan-lee-supervision")
        invitation = Invitation.objects.get(organization=org)
        self.assertEqual((invitation.org_role, invitation.credential_type, invitation.invited_by_id), ("administrator", "bcba", None))
        self.assertIn("Activation link:", out.getvalue())
        with self.assertRaises(Exception):
            call_command("provision_workspace", "--name", "Jordan Lee Supervision", "--admin-email", "x@akevra.test", stdout=StringIO())

    def test_switch_workspace_from_inside_the_app(self):
        alex = self.as_user("alex@akevra.test")
        self.assertEqual(alex.post("/api/v1/auth/workspace/switch").data["status"], "single_workspace")

        client = APIClient()
        login = client.post("/api/v1/auth/login", {"email": "admin@akevra.test", "password": PASSWORD}, format="json").data
        client.credentials(HTTP_AUTHORIZATION=f"Bearer {login['token']}")
        northshore = next(o for o in login["organizations"] if o["slug"] == "northshore-aba")
        client.post("/api/v1/auth/workspace/select", {"organization_id": northshore["id"]}, format="json")
        switched = client.post("/api/v1/auth/workspace/switch").data
        self.assertEqual(switched["status"], "workspace_required")
        self.assertEqual({o["slug"] for o in switched["organizations"]} >= {"northshore-aba", "summit-behavioral"}, True)
        # The old session has ended; the new one picks the other workspace
        self.assertEqual(client.get("/api/v1/relationships").status_code, 401)
        client.credentials(HTTP_AUTHORIZATION=f"Bearer {switched['token']}")
        summit = next(o for o in switched["organizations"] if o["slug"] == "summit-behavioral")
        entered = client.post("/api/v1/auth/workspace/select", {"organization_id": summit["id"]}, format="json").data
        self.assertEqual(entered["user"]["organization"]["slug"], "summit-behavioral")

    def test_availability_is_isolated_between_workspaces(self):
        slot = AvailabilitySlot.objects.create(organization=self.morgan.organization, owner=self.morgan, weekday=0,
                                               starts_at="09:00", ends_at="10:00")
        summit = Organization.objects.get(slug="summit-behavioral")

        def visible(org):
            with connection.cursor() as cursor:
                cursor.execute("SET ROLE akevra_app")
                cursor.execute("SELECT set_config('app.rls_bypass', 'off', false)")
                cursor.execute("SELECT set_config('app.current_organization_id', %s, false)", [str(org.id)])
                cursor.execute("SELECT count(*) FROM availability_slot WHERE id = %s", [str(slot.id)])
                count = cursor.fetchone()[0]
                cursor.execute("RESET ROLE")
            return count

        self.assertEqual((visible(self.morgan.organization), visible(summit)), (1, 0))
        outsider = self.as_user("summit.supervisor@akevra.test")
        self.assertEqual(outsider.get(f"/api/v1/relationships/{self.pd.id}/availability").status_code, 404)
