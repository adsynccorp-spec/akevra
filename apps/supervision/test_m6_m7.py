"""M6 #2–#4 and M7 Part A (A1–A3): rules by effective date, shortfall reminders, exports with review
comments and BT development records, relationship conclusion, no deletion, and departed Supervisors."""

from datetime import date
from decimal import Decimal

from django.core.management import call_command
from django.db import IntegrityError, InternalError, ProgrammingError, connection, transaction
from django.test import TestCase
from rest_framework.test import APIClient

from apps.accounts.models import CredentialType, UserAccount
from apps.core.tests import PASSWORD
from apps.supervision import periods, rules
from apps.supervision.compliance import load_rules
from apps.supervision.models import (
    ComplianceReminder,
    MonthlyCycle,
    ReviewComment,
    SupervisionTrack,
    SupervisoryRelationship,
)


class M6M7Tests(TestCase):
    @classmethod
    def setUpTestData(cls):
        call_command("seed_sprint0", verbosity=0)
        cls.jordan = UserAccount.objects.get(email="jordan@akevra.test")
        cls.alex = UserAccount.objects.get(email="alex@akevra.test")
        cls.morgan = UserAccount.objects.get(email="morgan@akevra.test")
        cls.rbt = SupervisoryRelationship.objects.get(supervisor=cls.jordan, supervisee=cls.alex)
        cls.pd = SupervisoryRelationship.objects.get(supervisor=cls.morgan, supervisee=cls.jordan)

    def as_user(self, email):
        client = APIClient()
        response = client.post("/api/v1/auth/login", {"email": email, "password": PASSWORD}, format="json")
        client.credentials(HTTP_AUTHORIZATION=f"Bearer {response.data['token']}")
        return client

    def session(self, client, rel, day, minutes):
        response = client.post(f"/api/v1/relationships/{rel.id}/sessions",
                               {"occurred_on": day, "duration_minutes": minutes, "session_type": "individual",
                                "meeting_method": "in_person", "topics": "Review"}, format="json")
        self.assertEqual(response.status_code, 201, response.data)
        return response.data

    # --- M6 #2: each month evaluated under the rules in force at its own date -----------------
    def test_new_rule_version_leaves_earlier_months_on_their_own_rules(self):
        org = self.rbt.organization
        track = SupervisionTrack.RBT_ONGOING
        rules.approve(org, self.jordan, track=track, effective_from=date(2026, 1, 1), note="First approval")
        rules.approve(org, self.jordan, track=track, effective_from=date(2026, 11, 1),
                      overrides={"current": {"min_supervision_percent": "6"}}, note="Raised to 6%")
        october, november = load_rules(track, date(2026, 10, 1), org.id), load_rules(track, date(2026, 11, 1), org.id)
        # Before the fix October fell back to the unapproved defaults
        self.assertTrue(october.source["approved"])
        self.assertEqual(october.number("min_supervision_percent"), Decimal("5"))
        self.assertEqual(november.number("min_supervision_percent"), Decimal("6"))
        self.assertIn("Kept for months before Nov 01, 2026", october.source["approval_note"])
        # Through the API: the October month reports the October rule set, with no schema change
        jordan = self.as_user("jordan@akevra.test")
        self.session(jordan, self.rbt, "2026-10-06", 60)
        periods.record_service_hours(self.rbt, self.alex, year=2026, month=10, hours="40")
        months = {(m["year"], m["month"]): m for m in jordan.get(f"/api/v1/relationships/{self.rbt.id}/compliance").data["months"]}
        self.assertEqual(months[(2026, 10)]["required_supervision_percent"], 5.0)

    # --- M6 #3: reminders are separate from compliance data ----------------------------------
    def test_reminders_resolve_and_dismiss_never_change_compliance(self):
        jordan, alex = self.as_user("jordan@akevra.test"), self.as_user("alex@akevra.test")
        self.session(jordan, self.rbt, "2026-09-08", 60)
        periods.record_service_hours(self.rbt, self.alex, year=2026, month=9, hours="100")
        before = jordan.get(f"/api/v1/relationships/{self.rbt.id}/compliance").data
        listed = jordan.get(f"/api/v1/relationships/{self.rbt.id}/reminders").data["reminders"]
        sept = [r for r in listed if (r["year"], r["month"]) == (2026, 9)]
        self.assertIn("supervision_percent", {r["check_key"] for r in sept})
        reminder = next(r for r in sept if r["check_key"] == "supervision_percent")
        self.assertEqual(reminder["status"], "open")
        # Listing again doesn't duplicate
        again = jordan.get(f"/api/v1/relationships/{self.rbt.id}/reminders").data["reminders"]
        self.assertEqual(len([r for r in again if r["check_key"] == "supervision_percent" and r["month"] == 9]), 1)

        self.assertEqual(alex.post(f"/api/v1/reminders/{reminder['id']}/resolve").status_code, 403)
        self.assertEqual(jordan.post(f"/api/v1/reminders/{reminder['id']}/dismiss", {}, format="json").status_code, 400)
        done = jordan.post(f"/api/v1/reminders/{reminder['id']}/dismiss", {"note": "Supervisee was on leave"}, format="json")
        self.assertEqual((done.status_code, done.data["status"]), (200, "dismissed"))
        other = next(r for r in sept if r["check_key"] != "supervision_percent")
        self.assertEqual(jordan.post(f"/api/v1/reminders/{other['id']}/resolve", {"note": "Scheduled"}, format="json").data["status"], "resolved")
        after = jordan.get(f"/api/v1/relationships/{self.rbt.id}/compliance").data
        self.assertEqual(before, after)

        # When the shortfall goes away an open reminder is cleared by the system
        self.session(jordan, self.rbt, "2026-08-04", 60)
        periods.record_service_hours(self.rbt, self.alex, year=2026, month=8, hours="100")
        jordan.get(f"/api/v1/relationships/{self.rbt.id}/reminders")
        cycle = MonthlyCycle.objects.get(relationship=self.rbt, year=2026, month=8)
        self.assertEqual(ComplianceReminder.objects.get(cycle=cycle, check_key="supervision_percent").status, "open")
        self.session(jordan, self.rbt, "2026-08-20", 300)
        jordan.get(f"/api/v1/relationships/{self.rbt.id}/reminders")
        self.assertEqual(ComplianceReminder.objects.get(cycle=cycle, check_key="supervision_percent").status, "cleared")

    # --- M6 #4: export with review comments and BT development records -----------------------
    def test_export_includes_review_comments_and_bt_development_records(self):
        jordan = self.as_user("jordan@akevra.test")
        session = self.session(jordan, self.rbt, "2026-09-08", 60)
        ReviewComment.objects.create(organization=self.rbt.organization, relationship=self.rbt, author=self.jordan,
                                     body="Great data sheet", target_entity_type="supervision_session", target_entity_id=session["id"])
        csv_text = jordan.get(f"/api/v1/relationships/{self.rbt.id}/export").content.decode()
        for part in ("AKEVRA relationship export v1", "MONTHLY COMPLIANCE", "REVIEW COMMENTS", "Great data sheet", "Session: Sep 08, 2026"):
            self.assertIn(part, csv_text)
        data = jordan.get(f"/api/v1/relationships/{self.rbt.id}/export?as=json").data
        self.assertEqual(data["comments"][0]["comment"], "Great data sheet")
        self.assertEqual(self.as_user("alex@akevra.test").get(f"/api/v1/relationships/{self.rbt.id}/export").status_code, 403)

        bt = UserAccount.objects.create(identity=type(self.alex.identity).objects.create_user(email="bt2@akevra.test", password=PASSWORD),
                                        organization=self.rbt.organization, email="bt2@akevra.test", first_name="Bea",
                                        last_name="Tech", credential_type=CredentialType.BT)
        bt_rel = SupervisoryRelationship.objects.create(organization=self.rbt.organization, supervisor=self.jordan, supervisee=bt,
                                                        supervision_track=SupervisionTrack.BT_DEVELOPMENT, started_on=date(2026, 9, 1))
        self.session(jordan, bt_rel, "2026-09-10", 45)
        text = jordan.get(f"/api/v1/relationships/{bt_rel.id}/export").content.decode()
        self.assertIn("BT DEVELOPMENT RECORDS — SESSIONS", text)
        self.assertIn("2026-09-10", text)
        self.assertNotIn("MONTHLY COMPLIANCE", text)

    # --- M7 A1: conclusion with a reason; the record stays readable ---------------------------
    def test_conclude_relationship_with_reason(self):
        jordan, alex = self.as_user("jordan@akevra.test"), self.as_user("alex@akevra.test")
        self.session(jordan, self.rbt, "2026-09-08", 60)
        upcoming = jordan.post(f"/api/v1/relationships/{self.rbt.id}/appointments",
                               {"starts_at": "2030-01-10T15:00:00Z", "ends_at": "2030-01-10T16:00:00Z"}, format="json").data["appointments"][0]
        url = f"/api/v1/relationships/{self.rbt.id}/conclude"
        self.assertEqual(alex.post(url, {"reason": "reassignment"}, format="json").status_code, 403)
        self.assertEqual(jordan.post(url, {"reason": ""}, format="json").status_code, 400)
        self.assertEqual(jordan.post(url, {"reason": "other"}, format="json").status_code, 400)
        self.assertEqual(jordan.patch(f"/api/v1/relationships/{self.rbt.id}", {"status": "concluded"}, format="json").status_code, 400)
        done = jordan.post(url, {"reason": "certification_achieved", "concluded_on": "2026-10-31", "notes": "Passed BCaBA"}, format="json")
        self.assertEqual(done.status_code, 200, done.data)
        self.assertEqual((done.data["status"], done.data["ended_on"]), ("concluded", "2026-10-31"))
        self.assertEqual(done.data["conclusion"]["reason_label"], "Certification achieved")
        self.assertEqual(jordan.post(url, {"reason": "reassignment"}, format="json").status_code, 400)
        # Still fully readable by both parties
        for client in (jordan, alex):
            self.assertEqual(client.get(f"/api/v1/relationships/{self.rbt.id}").status_code, 200)
            self.assertEqual(len(client.get(f"/api/v1/relationships/{self.rbt.id}/sessions").data["sessions"]), 1)
        self.assertEqual(jordan.get(f"/api/v1/appointments/{upcoming['id']}").data["status"], "cancelled")
        self.assertEqual(jordan.post(f"/api/v1/relationships/{self.rbt.id}/appointments",
                                     {"starts_at": "2030-02-10T15:00:00Z", "ends_at": "2030-02-10T16:00:00Z"}, format="json").status_code, 400)

    # --- M7 A2: no permanent deletion -------------------------------------------------------
    def test_no_permanent_deletion_at_api_or_database(self):
        jordan = self.as_user("jordan@akevra.test")
        self.assertIn(jordan.delete(f"/api/v1/relationships/{self.rbt.id}").status_code, (403, 405))
        session = self.session(jordan, self.rbt, "2026-09-08", 60)
        self.assertIn(jordan.delete(f"/api/v1/sessions/{session['id']}").status_code, (403, 405))
        comment = ReviewComment.objects.create(organization=self.rbt.organization, relationship=self.rbt, author=self.jordan,
                                               body="x", target_entity_type="supervision_session", target_entity_id=session["id"])
        for table, pk in (("supervisory_relationship", self.rbt.id), ("supervision_session", session["id"]), ("review_comment", comment.id)):
            with self.assertRaises((IntegrityError, InternalError, ProgrammingError)), transaction.atomic():
                with connection.cursor() as cursor:
                    cursor.execute(f"DELETE FROM {table} WHERE id = %s", [str(pk)])

    # --- M7 A3: a departed Supervisor's records stay readable to authorized viewers ------------
    def test_departed_supervisor_records_remain_accessible(self):
        morgan = self.as_user("morgan@akevra.test")
        self.session(morgan, self.pd, "2026-09-09", 60)
        admin = self.as_user("settings.admin@akevra.test")
        gone = admin.post(f"/api/v1/users/members/{self.morgan.id}/depart", {"notes": "Moved away"}, format="json")
        self.assertEqual(gone.status_code, 200, gone.data)
        self.assertGreaterEqual(gone.data["concluded_relationships"], 1)
        self.pd.refresh_from_db()
        self.assertEqual(self.pd.status, "concluded")
        # Morgan can no longer sign in to this workspace
        login = APIClient().post("/api/v1/auth/login", {"email": "morgan@akevra.test", "password": PASSWORD}, format="json")
        self.assertNotEqual(login.data.get("status"), "authenticated")
        # The participant and the Clinical Director still read the history Morgan wrote
        for email in ("jordan@akevra.test", "director@akevra.test"):
            client = self.as_user(email)
            rel = client.get(f"/api/v1/relationships/{self.pd.id}")
            self.assertEqual(rel.status_code, 200, email)
            self.assertEqual(rel.data["supervisor"]["name"], "Morgan Chen")
            sessions = client.get(f"/api/v1/relationships/{self.pd.id}/sessions").data["sessions"]
            self.assertEqual(sessions[0]["documented_by"]["name"] if isinstance(sessions[0]["documented_by"], dict) else sessions[0]["documented_by"], "Morgan Chen")
        # Someone who never had access still doesn't
        self.assertEqual(self.as_user("summit.supervisor@akevra.test").get(f"/api/v1/relationships/{self.pd.id}").status_code, 404)
