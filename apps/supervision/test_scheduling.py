from django.core.management import call_command
from django.test import TestCase
from rest_framework.test import APIClient

from apps.accounts.models import UserAccount
from apps.core.tests import PASSWORD
from apps.supervision.models import SupervisoryRelationship


class SchedulingAcceptanceTests(TestCase):
    """Maps 1:1 to the Scheduling & Supervision Sessions milestone."""

    @classmethod
    def setUpTestData(cls):
        call_command("seed_sprint0", verbosity=0)
        cls.jordan = UserAccount.objects.get(email="jordan@akevra.test")
        cls.alex = UserAccount.objects.get(email="alex@akevra.test")
        cls.rbt = SupervisoryRelationship.objects.get(supervisor=cls.jordan, supervisee=cls.alex)

    def as_user(self, email):
        client = APIClient()
        response = client.post("/api/v1/auth/login", {"email": email, "password": PASSWORD}, format="json")
        client.credentials(HTTP_AUTHORIZATION=f"Bearer {response.data['token']}")
        return client

    def schedule(self, client, start="2026-10-20T10:00:00Z", end="2026-10-20T11:00:00Z", **extra):
        return client.post(f"/api/v1/relationships/{self.rbt.id}/appointments",
                           {"starts_at": start, "ends_at": end, "session_type": "individual", "meeting_method": "in_person", **extra},
                           format="json")

    def figures(self, client):
        """Everything compliance is built from for this relationship."""
        compliance = client.get(f"/api/v1/relationships/{self.rbt.id}/compliance").data
        cycles = client.get(f"/api/v1/relationships/{self.rbt.id}/cycles").data["cycles"]
        return compliance, [(c["year"], c["month"], c["counts"]) for c in cycles]

    # 1. Formal session records linked to their monthly cycle
    def test_01_session_records_link_to_cycle_and_appointment(self):
        jordan = self.as_user("jordan@akevra.test")
        appointment = self.schedule(jordan).data["appointments"][0]
        session = jordan.post(f"/api/v1/relationships/{self.rbt.id}/sessions",
                              {"occurred_on": "2026-10-20", "start_time": "10:00", "end_time": "11:00", "session_type": "individual",
                               "meeting_method": "in_person", "topics": "BIP", "appointment_id": appointment["id"]}, format="json")
        self.assertEqual(session.status_code, 201, session.data)
        self.assertEqual(session.data["appointment_id"], appointment["id"])
        cycle = jordan.get(f"/api/v1/cycles/{session.data['cycle_id']}").data
        self.assertEqual((cycle["year"], cycle["month"]), (2026, 10))
        self.assertEqual([s["id"] for s in cycle["sessions"]], [session.data["id"]])
        held = jordan.get(f"/api/v1/appointments/{appointment['id']}").data
        self.assertEqual(held["status"], "completed")
        self.assertEqual(held["session"]["id"], session.data["id"])

    # 2. Scheduling, rescheduling, cancellation and batch creation
    def test_02_schedule_reschedule_cancel_and_batch(self):
        jordan = self.as_user("jordan@akevra.test")
        alex = self.as_user("alex@akevra.test")
        self.assertEqual(self.schedule(alex).status_code, 403)
        self.assertEqual(self.schedule(jordan, end="2026-10-20T09:00:00Z").status_code, 400)

        one = self.schedule(jordan, agenda="Review BIP data").data["appointments"][0]
        self.assertEqual(one["status"], "scheduled")
        confirmed = alex.post(f"/api/v1/appointments/{one['id']}/confirm")
        self.assertEqual(confirmed.data["status"], "confirmed")

        moved = jordan.patch(f"/api/v1/appointments/{one['id']}",
                             {"starts_at": "2026-10-22T14:00:00Z", "ends_at": "2026-10-22T15:00:00Z", "reason": "Clinic closed"},
                             format="json")
        self.assertEqual(moved.status_code, 200, moved.data)
        self.assertEqual(moved.data["status"], "scheduled")  # a new time needs re-confirming
        last = moved.data["events"][-1]
        self.assertEqual((last["action"], last["reason"]), ("rescheduled", "Clinic closed"))
        self.assertTrue(last["previous_starts_at"].startswith("2026-10-20T10:00"))
        self.assertTrue(last["starts_at"].startswith("2026-10-22T14:00"))

        self.assertEqual(alex.post(f"/api/v1/appointments/{one['id']}/cancel", {"reason": "x"}, format="json").status_code, 403)
        self.assertEqual(jordan.post(f"/api/v1/appointments/{one['id']}/cancel", {}, format="json").status_code, 400)
        cancelled = jordan.post(f"/api/v1/appointments/{one['id']}/cancel", {"reason": "Supervisee ill"}, format="json")
        self.assertEqual((cancelled.data["status"], cancelled.data["cancel_reason"]), ("cancelled", "Supervisee ill"))
        self.assertEqual(jordan.patch(f"/api/v1/appointments/{one['id']}", {"agenda": "x"}, format="json").status_code, 400)
        self.assertEqual([e["action"] for e in cancelled.data["events"]], ["scheduled", "confirmed", "rescheduled", "cancelled"])

        batch = self.schedule(jordan, start="2026-11-03T10:00:00Z", end="2026-11-03T11:00:00Z", repeat_weeks=3)
        self.assertEqual(batch.status_code, 201, batch.data)
        rows = batch.data["appointments"]
        self.assertEqual([r["starts_at"][:10] for r in rows], ["2026-11-03", "2026-11-10", "2026-11-17", "2026-11-24"])
        self.assertEqual(len({r["series_id"] for r in rows}), 1)
        self.assertEqual(len(alex.get("/api/v1/appointments").data["appointments"]), 5)

    # 3. Product boundary: no appointment state changes any compliance figure
    def test_03_appointments_never_change_compliance_figures(self):
        jordan = self.as_user("jordan@akevra.test")
        alex = self.as_user("alex@akevra.test")
        alex.put(f"/api/v1/relationships/{self.rbt.id}/service-hours", {"year": 2026, "month": 10, "hours": "40"}, format="json")
        jordan.post(f"/api/v1/relationships/{self.rbt.id}/sessions",
                    {"occurred_on": "2026-10-05", "duration_minutes": 60, "session_type": "individual", "client_observation": True},
                    format="json")
        baseline = self.figures(jordan)

        appointment = self.schedule(jordan, start="2026-10-21T10:00:00Z", end="2026-10-21T12:00:00Z").data["appointments"][0]
        self.assertEqual(self.figures(jordan), baseline)
        alex.post(f"/api/v1/appointments/{appointment['id']}/confirm")
        self.assertEqual(self.figures(jordan), baseline)
        alex.put(f"/api/v1/appointments/{appointment['id']}/prep-note", {"body": "I'd like to review transitions."}, format="json")
        self.assertEqual(self.figures(jordan), baseline)
        jordan.post(f"/api/v1/appointments/{appointment['id']}/complete")
        self.assertEqual(self.figures(jordan), baseline)
        batch = self.schedule(jordan, start="2026-10-23T10:00:00Z", end="2026-10-23T11:00:00Z", repeat_weeks=1).data["appointments"]
        jordan.post(f"/api/v1/appointments/{batch[0]['id']}/no-show")
        jordan.post(f"/api/v1/appointments/{batch[1]['id']}/cancel", {"reason": "Holiday"}, format="json")
        self.assertEqual(self.figures(jordan), baseline)

        month = baseline[0]["months"][-1]
        self.assertEqual((month["supervision_hours"], month["contacts"]), (1.0, 1))

    # 4. Pre-supervision preparation notes are not compliance-bearing
    def test_04_prep_notes_are_supervisee_only_and_not_compliance_bearing(self):
        jordan = self.as_user("jordan@akevra.test")
        alex = self.as_user("alex@akevra.test")
        appointment = self.schedule(jordan).data["appointments"][0]
        baseline = self.figures(jordan)
        self.assertEqual(jordan.put(f"/api/v1/appointments/{appointment['id']}/prep-note", {"body": "x"}, format="json").status_code, 403)
        self.assertEqual(alex.put(f"/api/v1/appointments/{appointment['id']}/prep-note", {"body": " "}, format="json").status_code, 400)
        saved = alex.put(f"/api/v1/appointments/{appointment['id']}/prep-note", {"body": "Focus on behaviour reduction."}, format="json")
        self.assertEqual(saved.status_code, 200, saved.data)
        updated = alex.put(f"/api/v1/appointments/{appointment['id']}/prep-note", {"body": "Focus on BIP data."}, format="json")
        self.assertEqual(updated.data["id"], saved.data["id"])  # one note per appointment, edited in place
        self.assertEqual(jordan.get(f"/api/v1/appointments/{appointment['id']}").data["prep_note"]["body"], "Focus on BIP data.")
        self.assertEqual(self.figures(jordan), baseline)

        jordan.post(f"/api/v1/appointments/{appointment['id']}/cancel", {"reason": "Moved"}, format="json")
        self.assertEqual(alex.put(f"/api/v1/appointments/{appointment['id']}/prep-note", {"body": "late"}, format="json").status_code, 400)

    # 5. Only completed, documented, qualifying supervision counts
    def test_05_only_completed_qualifying_sessions_count(self):
        jordan = self.as_user("jordan@akevra.test")
        alex = self.as_user("alex@akevra.test")
        alex.put(f"/api/v1/relationships/{self.rbt.id}/service-hours", {"year": 2026, "month": 10, "hours": "40"}, format="json")
        month = lambda: jordan.get(f"/api/v1/relationships/{self.rbt.id}/compliance").data["months"][-1]
        base = {"occurred_on": "2026-10-06", "duration_minutes": 60, "session_type": "individual"}

        phone = jordan.post(f"/api/v1/relationships/{self.rbt.id}/sessions", {**base, "meeting_method": "phone"}, format="json")
        self.assertFalse(phone.data["qualifies"])
        jordan.post(f"/api/v1/relationships/{self.rbt.id}/sessions", {**base, "meeting_method": "asynchronous"}, format="json")
        jordan.post(f"/api/v1/relationships/{self.rbt.id}/sessions", {**base, "meeting_method": "in_person", "draft": True}, format="json")
        held = self.schedule(jordan).data["appointments"][0]
        jordan.post(f"/api/v1/appointments/{held['id']}/complete")
        self.assertEqual((month()["supervision_hours"], month()["contacts"]), (0.0, 0))

        counted = jordan.post(f"/api/v1/relationships/{self.rbt.id}/sessions", {**base, "meeting_method": "remote"}, format="json")
        self.assertTrue(counted.data["qualifies"])
        self.assertEqual((month()["supervision_hours"], month()["contacts"]), (1.0, 1))

    def test_06_approved_rule_sets_replace_pending_defaults(self):
        jordan = self.as_user("jordan@akevra.test")
        alex = self.as_user("alex@akevra.test")
        alex.put(f"/api/v1/relationships/{self.rbt.id}/service-hours", {"year": 2026, "month": 10, "hours": "40"}, format="json")
        rules = lambda: jordan.get(f"/api/v1/relationships/{self.rbt.id}/compliance").data["months"][-1]["rules"]
        self.assertFalse(rules()["approved"])
        call_command("approve_compliance_rules", name="AKEVRA Approved Oct 2026", effective_from="2026-01-01", verbosity=0)
        self.assertTrue(rules()["approved"])
        self.assertEqual(rules()["name"], "AKEVRA Approved Oct 2026")

    # 7. Notifications (bell + email) for scheduling and preparation; reminders; clearer messages
    def test_07_notifications_reminders_and_messages(self):
        from datetime import timedelta

        from django.core import mail
        from django.utils import timezone

        from apps.supervision.notifications import send_preparation_reminders

        jordan = self.as_user("jordan@akevra.test")
        alex = self.as_user("alex@akevra.test")
        start = timezone.now() + timedelta(hours=24)
        appointment = self.schedule(jordan, start=start.isoformat(), end=(start + timedelta(hours=1)).isoformat()).data["appointments"][0]

        inbox = alex.get("/api/v1/notifications").data
        self.assertEqual(inbox["unread"], 1)
        self.assertIn("Supervision scheduled", inbox["notifications"][0]["title"])
        self.assertTrue(inbox["notifications"][0]["link"].endswith("?tab=sessions"))
        self.assertTrue(any("Supervision scheduled" in m.subject for m in mail.outbox))

        # One reminder per appointment while preparation is pending
        self.assertEqual(send_preparation_reminders(), 1)
        self.assertEqual(send_preparation_reminders(), 0)
        self.assertEqual(alex.get("/api/v1/notifications").data["unread"], 2)

        # Submitting preparation notifies the Supervisor once
        alex.put(f"/api/v1/appointments/{appointment['id']}/prep-note", {"body": "Questions on BIP"}, format="json")
        alex.put(f"/api/v1/appointments/{appointment['id']}/prep-note", {"body": "Questions on BIP (edited)"}, format="json")
        titles = [n["title"] for n in jordan.get("/api/v1/notifications").data["notifications"]]
        self.assertEqual(sum("submitted supervision preparation" in t for t in titles), 1)

        # Read state is per person
        first = alex.get("/api/v1/notifications").data["notifications"][0]
        self.assertTrue(alex.post(f"/api/v1/notifications/{first['id']}/read").data["read"])
        self.assertEqual(jordan.post(f"/api/v1/notifications/{first['id']}/read").status_code, 404)
        alex.post("/api/v1/notifications/read-all")
        self.assertEqual(alex.get("/api/v1/notifications").data["unread"], 0)

        # A repeated no-show says so plainly
        jordan.post(f"/api/v1/appointments/{appointment['id']}/no-show")
        again = jordan.post(f"/api/v1/appointments/{appointment['id']}/no-show")
        self.assertEqual(again.status_code, 400)
        self.assertIn("already marked as No-Show", str(again.data))

        # A time-only reschedule keeps the old and new times in history
        other = self.schedule(jordan, start="2026-11-03T10:00:00Z", end="2026-11-03T11:00:00Z").data["appointments"][0]
        moved = jordan.patch(f"/api/v1/appointments/{other['id']}", {"starts_at": "2026-11-03T14:00:00Z", "ends_at": "2026-11-03T15:00:00Z", "reason": "clash"}, format="json").data
        event = moved["events"][-1]
        self.assertEqual((event["previous_starts_at"][:16], event["starts_at"][:16]), ("2026-11-03T10:00", "2026-11-03T14:00"))
