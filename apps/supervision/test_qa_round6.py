"""QA round 6: future-dated fieldwork hours, previous-session context, end-of-session prompt support and the
pending-documentation reminder."""

from datetime import date, datetime, timedelta, timezone as dt_timezone
from unittest import mock

from django.core.management import call_command
from django.test import TestCase
from django.utils import timezone
from rest_framework.test import APIClient

from apps.accounts.models import UserAccount
from apps.audit.models import AuditEvent
from apps.core.tests import PASSWORD
from apps.supervision.compliance import organization_today
from apps.supervision.models import HoursEntry, SupervisionTrack, SupervisoryRelationship
from apps.supervision.monthly import cycle_for_date


class FutureDatedHoursTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        call_command("seed_sprint0", verbosity=0)
        cls.jordan = UserAccount.objects.get(email="jordan@akevra.test")
        cls.casey = UserAccount.objects.get(email="casey@akevra.test")
        cls.rel = SupervisoryRelationship.objects.create(
            organization=cls.jordan.organization, supervisor=cls.jordan, supervisee=cls.casey,
            supervision_track=SupervisionTrack.SUPERVISED_FIELDWORK, fieldwork_subtype="supervised",
            started_on=date(2026, 6, 1), fieldwork_requirements="bacb_2022", fieldwork_standard_basis="application_year",
        )

    def as_user(self, email):
        client = APIClient()
        token = client.post("/api/v1/auth/login", {"email": email, "password": PASSWORD}, format="json").data["token"]
        client.credentials(HTTP_AUTHORIZATION=f"Bearer {token}")
        return client

    def url(self):
        return f"/api/v1/relationships/{self.rel.id}/hours"

    def test_future_dated_hours_cant_be_logged_corrected_or_verified(self):
        casey, jordan = self.as_user("casey@akevra.test"), self.as_user("jordan@akevra.test")
        today = organization_today(self.rel.organization)
        tomorrow = (today + timedelta(days=1)).isoformat()
        refused = casey.post(self.url(), {"occurred_on": tomorrow, "hours": "1", "kind": "independent"}, format="json")
        self.assertEqual(refused.status_code, 400)
        self.assertIn("in the future", str(refused.data))
        self.assertFalse(HoursEntry.objects.filter(cycle__relationship=self.rel).exists())
        # Today is allowed; moving it to a future date is refused
        ok = casey.post(self.url(), {"occurred_on": today.isoformat(), "hours": "1", "kind": "independent"}, format="json")
        self.assertEqual(ok.status_code, 201, ok.data)
        moved = casey.patch(f"/api/v1/hours/{ok.data['id']}", {"occurred_on": tomorrow}, format="json")
        self.assertEqual(moved.status_code, 400)
        self.assertIn("in the future", str(moved.data))
        # Verification is refused on the backend for a future-dated pending entry (one saved before the check)
        cycle = cycle_for_date(self.rel, today + timedelta(days=40))
        legacy = HoursEntry.objects.create(organization=self.rel.organization, cycle=cycle, occurred_on=today + timedelta(days=40),
                                           attested_on=today, hours=1, kind="independent", logged_by=self.casey, status="pending")
        verify = jordan.post(f"/api/v1/hours/{legacy.id}/verify")
        self.assertEqual(verify.status_code, 400)
        self.assertIn("hasn't happened yet", str(verify.data))
        legacy.refresh_from_db()
        self.assertEqual(legacy.status, "pending")
        listed = {e["id"]: e for e in jordan.get(self.url()).data["entries"]}
        self.assertTrue(listed[str(legacy.id)]["future_dated"])
        self.assertFalse(listed[ok.data["id"]]["future_dated"])

    def test_previously_verified_future_records_are_kept_unchanged_and_never_count_early(self):
        jordan = self.as_user("jordan@akevra.test")
        today = organization_today(self.rel.organization)
        future = today + timedelta(days=25)
        cycle = cycle_for_date(self.rel, future)
        entry = HoursEntry.objects.create(organization=self.rel.organization, cycle=cycle, occurred_on=future, attested_on=today,
                                          hours=1, kind="independent", logged_by=self.casey, status="verified",
                                          verified_by=self.jordan, verified_at=timezone.now())
        AuditEvent.objects.create(organization_id=self.rel.organization_id, action="hours.verify", entity_type="hours_entry",
                                  entity_id=str(entry.id))
        data = jordan.get(f"/api/v1/relationships/{self.rel.id}/compliance").data
        month = next(m for m in data["months"] if (m["year"], m["month"]) == (future.year, future.month))
        self.assertEqual(month["total_hours"], 0.0)                      # not counted before its date
        self.assertEqual(data["cumulative"]["supervised_hours"], 0.0)
        entry.refresh_from_db()
        self.assertEqual((entry.status, str(entry.occurred_on)), ("verified", str(future)))   # record untouched
        self.assertTrue(AuditEvent.objects.filter(entity_id=str(entry.id), action="hours.verify").exists())
        # Once its date arrives it counts as usual
        with mock.patch("apps.supervision.compliance.organization_today", return_value=future), \
                mock.patch("apps.supervision.monthly.organization_today", return_value=future):
            data = jordan.get(f"/api/v1/relationships/{self.rel.id}/compliance").data
        month = next(m for m in data["months"] if (m["year"], m["month"]) == (future.year, future.month))
        self.assertEqual(month["total_hours"], 1.0)

    def test_today_follows_the_organization_time_zone_not_utc(self):
        org = self.rel.organization
        org.time_zone = "America/Los_Angeles"
        org.save(update_fields=["time_zone", "updated_at"])
        # 03:00 UTC on Dec 16 is still the evening of Dec 15 in Los Angeles
        late_evening = datetime(2026, 12, 16, 3, 0, tzinfo=dt_timezone.utc)
        with mock.patch("django.utils.timezone.now", return_value=late_evening):
            self.assertEqual(organization_today(org), date(2026, 12, 15))
            casey = self.as_user("casey@akevra.test")
            ok = casey.post(self.url(), {"occurred_on": "2026-12-15", "hours": "1", "kind": "independent"}, format="json")
            self.assertEqual(ok.status_code, 201, ok.data)
            self.assertEqual(ok.data["attested_on"], "2026-12-15")   # local date, not the UTC date
            ahead = casey.post(self.url(), {"occurred_on": "2026-12-16", "hours": "1", "kind": "independent"}, format="json")
            self.assertEqual(ahead.status_code, 400)


class SessionWorkflowRefinementTests(TestCase):
    """Message 2: previous session context, pending-documentation reminder, consistent note status, sections that apply."""

    @classmethod
    def setUpTestData(cls):
        call_command("seed_sprint0", verbosity=0)
        cls.jordan = UserAccount.objects.get(email="jordan@akevra.test")
        cls.alex = UserAccount.objects.get(email="alex@akevra.test")
        cls.rbt = SupervisoryRelationship.objects.get(supervisor=cls.jordan, supervisee=cls.alex)

    def setUp(self):
        from apps.supervision import ai
        patcher = mock.patch.object(ai, "_api_key", return_value="")
        patcher.start()
        self.addCleanup(patcher.stop)
        self.jordan_c = FutureDatedHoursTests.as_user(self, "jordan@akevra.test")

    def session(self, day, start="10:00"):
        end = f"{int(start[:2]) + 1:02d}:00"
        made = self.jordan_c.post(f"/api/v1/relationships/{self.rbt.id}/sessions", {
            "occurred_on": day, "start_time": start, "end_time": end, "session_type": "individual",
            "meeting_method": "in_person", "topics": "DTT review", "activities": "Observed DTT"}, format="json")
        self.assertEqual(made.status_code, 201, made.data)
        return made.data["id"]

    def document(self, sid, approve=True):
        base = f"/api/v1/sessions/{sid}/documentation"
        self.jordan_c.put(base, {"observed": "Accurate prompting", "feedback_given": "Modelled faster recording",
                                 "changes_assigned": "Record data within the trial", "next_focus": ""}, format="json")
        self.jordan_c.post(f"{base}/generate", {}, format="json")
        if approve:
            for kind in ("supervisor_note", "supervisee_feedback"):
                self.assertEqual(self.jordan_c.post(f"{base}/{kind}/approve", {}, format="json").status_code, 200)
        return base

    def test_previous_sessions_approved_feedback_and_follow_up_show_on_the_next_session(self):
        first = self.session("2026-10-06")
        self.document(first)
        second = self.session("2026-10-13")
        data = self.jordan_c.get(f"/api/v1/sessions/{second}/documentation").data
        previous = data["previous"]
        self.assertEqual((previous["session_id"], previous["occurred_on"]), (first, "2026-10-06"))
        self.assertIn("Record data within the trial", " ".join(previous["follow_up"]))
        self.assertEqual(previous["feedback"]["content"]["follow_up"], "Record data within the trial")
        # Unapproved drafts are never shown as previous feedback
        third = self.session("2026-10-20")
        self.document(second, approve=False)
        self.assertEqual(self.jordan_c.get(f"/api/v1/sessions/{third}/documentation").data["previous"]["session_id"], first)
        # The supervisee never gets this context
        alex = FutureDatedHoursTests.as_user(self, "alex@akevra.test")
        self.assertNotIn("previous", alex.get(f"/api/v1/sessions/{third}/documentation").data)

    def test_sections_that_dont_apply_are_left_empty_not_filled(self):
        sid = self.session("2026-10-07")
        base = self.document(sid, approve=False)
        note = self.jordan_c.get(base).data["outputs"]["supervisor_note"]["content"]
        self.assertNotIn("Not documented.", note.values())
        self.assertTrue(note["observations"])

    def test_note_status_is_listed_with_each_session(self):
        approved, drafted, untouched = self.session("2026-10-08"), self.session("2026-10-09"), self.session("2026-10-12")
        self.document(approved)
        self.document(drafted, approve=False)
        listed = {s["id"]: s["note_status"] for s in self.jordan_c.get(f"/api/v1/relationships/{self.rbt.id}/sessions").data["sessions"]}
        self.assertEqual((listed[approved], listed[drafted], listed[untouched]), ("approved", "draft", "not_started"))

    def test_pending_documentation_reminder_is_sent_once(self):
        from apps.supervision.models import Notification
        from apps.supervision.notifications import send_documentation_reminders

        pending = self.session("2026-10-14")
        done = self.session("2026-10-15")
        self.document(done)
        later = timezone.now() + timedelta(hours=30)
        self.assertEqual(send_documentation_reminders(now=later), 1)
        reminder = Notification.objects.get(kind="documentation_reminder")
        self.assertEqual(reminder.recipient_id, self.jordan.id)
        self.assertIn(pending, reminder.link)
        self.assertEqual(send_documentation_reminders(now=later), 0)          # once only
        self.assertEqual(send_documentation_reminders(now=timezone.now()), 0)  # not before 24 hours


class SingleEntryTests(TestCase):
    """A1: what happened is written once, as the guided answers; the session record keeps time, method and type."""

    @classmethod
    def setUpTestData(cls):
        call_command("seed_sprint0", verbosity=0)
        cls.jordan = UserAccount.objects.get(email="jordan@akevra.test")
        cls.alex = UserAccount.objects.get(email="alex@akevra.test")
        cls.rbt = SupervisoryRelationship.objects.get(supervisor=cls.jordan, supervisee=cls.alex)

    def setUp(self):
        from apps.supervision import ai
        patcher = mock.patch.object(ai, "_api_key", return_value="")
        patcher.start()
        self.addCleanup(patcher.stop)
        self.c = FutureDatedHoursTests.as_user(self, "jordan@akevra.test")
        self.url = f"/api/v1/relationships/{self.rbt.id}/sessions"
        self.base = {"occurred_on": "2026-10-06", "start_time": "10:00", "end_time": "11:00", "session_type": "individual",
                     "meeting_method": "in_person"}

    def test_guided_answers_are_the_only_narrative_entry(self):
        from apps.supervision.models import SupervisionSession

        nothing = self.c.post(self.url, self.base, format="json")
        self.assertEqual(nothing.status_code, 400)
        self.assertIn("what happened", str(nothing.data))
        made = self.c.post(self.url, {**self.base, "observed": "Accurate prompting during DTT",
                                      "feedback_given": "Modelled faster data recording",
                                      "changes_assigned": "Record data within the trial", "next_focus": "Data timing"}, format="json")
        self.assertEqual(made.status_code, 201, made.data)
        session = SupervisionSession.objects.get(pk=made.data["id"])
        # The record keeps time, method and type only; the narrative lives once, in the guided answers
        self.assertEqual((session.topics, session.activities, session.feedback, session.follow_up), ("", "", "", ""))
        self.assertEqual((session.status, str(session.start_time)[:5], session.meeting_method), ("completed", "10:00", "in_person"))
        doc = self.c.get(f"/api/v1/sessions/{session.id}/documentation").data
        self.assertTrue(doc["exists"])
        self.assertEqual(doc["answers"]["observed"], "Accurate prompting during DTT")
        self.assertNotIn("prefill", doc)
        drafted = self.c.post(f"/api/v1/sessions/{session.id}/documentation/generate", {}, format="json")
        self.assertEqual(drafted.status_code, 200)
        self.assertIn("Accurate prompting", drafted.data["outputs"]["supervisor_note"]["content"]["observations"])
        listed = {s["id"]: s for s in self.c.get(self.url).data["sessions"]}
        self.assertEqual(listed[str(session.id)]["review_flags"], [])     # documented, so not flagged

    def test_a_draft_keeps_its_answers_and_completes_without_retyping(self):
        draft = self.c.post(self.url, {**self.base, "occurred_on": "2026-10-08", "draft": True,
                                       "observed": "Observed two DTT programs"}, format="json")
        self.assertEqual(draft.status_code, 201, draft.data)
        doc = self.c.get(f"/api/v1/sessions/{draft.data['id']}/documentation").data
        self.assertEqual(doc["answers"]["observed"], "Observed two DTT programs")
        done = self.c.patch(f"/api/v1/sessions/{draft.data['id']}", {"complete": True}, format="json")
        self.assertEqual(done.status_code, 200, done.data)
        self.assertEqual(done.data["status"], "completed")
