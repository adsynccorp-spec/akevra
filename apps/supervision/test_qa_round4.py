"""QA round 4 (M6/M7 interim findings): record integrity, reminders and the renamed rating scale."""

from datetime import date, timedelta
from unittest import mock

from django.core.management import call_command
from django.test import TestCase
from django.utils import timezone
from rest_framework.test import APIClient

from apps.accounts.models import UserAccount
from apps.core.tests import PASSWORD
from apps.supervision import reminders
from apps.supervision.assessments import serialize_assessment
from apps.supervision.models import (
    AssessmentStatus,
    CompetencyAssessment,
    ComplianceReminder,
    SupervisionSession,
    SupervisoryRelationship,
)
from apps.supervision.monthly import cycle_for_date
from apps.supervision.services import create_development_plan


class QARound4Tests(TestCase):
    @classmethod
    def setUpTestData(cls):
        call_command("seed_sprint0", verbosity=0)
        cls.jordan = UserAccount.objects.get(email="jordan@akevra.test")
        cls.alex = UserAccount.objects.get(email="alex@akevra.test")
        cls.rbt = SupervisoryRelationship.objects.get(supervisor=cls.jordan, supervisee=cls.alex)

    def as_user(self, email):
        client = APIClient()
        token = client.post("/api/v1/auth/login", {"email": email, "password": PASSWORD}, format="json").data["token"]
        client.credentials(HTTP_AUTHORIZATION=f"Bearer {token}")
        return client

    def post_session(self, client, **body):
        base = {"occurred_on": "2026-10-14", "start_time": "10:00", "end_time": "11:00", "session_type": "individual"}
        return client.post(f"/api/v1/relationships/{self.rbt.id}/sessions", {**base, **body}, format="json")

    def test_completed_record_needs_method_and_documentation_quick_log_is_a_draft(self):
        jordan = self.as_user("jordan@akevra.test")
        no_method = self.post_session(jordan, topics="Reviewed BIP")
        self.assertEqual(no_method.status_code, 400)
        self.assertIn("meeting method", str(no_method.data))
        no_notes = self.post_session(jordan, meeting_method="in_person")
        self.assertEqual(no_notes.status_code, 400)
        self.assertIn("what happened in the session", str(no_notes.data))
        # A Quick Log is a preliminary draft: allowed with little detail, never counted
        quick = self.post_session(jordan, duration_minutes=60, draft=True)
        self.assertEqual(quick.status_code, 201, quick.data)
        self.assertEqual(SupervisionSession.objects.get(pk=quick.data["id"]).status, "draft")
        # Completing the draft needs the same safeguards
        done = jordan.patch(f"/api/v1/sessions/{quick.data['id']}", {"complete": True}, format="json")
        self.assertEqual(done.status_code, 400)
        done = jordan.patch(f"/api/v1/sessions/{quick.data['id']}",
                            {"complete": True, "meeting_method": "in_person", "topics": "Reviewed BIP"}, format="json")
        self.assertEqual(done.status_code, 200, done.data)
        # The same event can't be documented twice
        again = self.post_session(jordan, meeting_method="in_person", topics="Reviewed BIP")
        self.assertEqual(again.status_code, 400)
        self.assertIn("already documented", str(again.data))

    def test_no_reminders_for_future_months_or_unfinished_service_hours(self):
        today = timezone.localdate()
        next_month = (today.replace(day=1) + timedelta(days=32)).replace(day=1)
        future = cycle_for_date(self.rbt, next_month)
        current = cycle_for_date(self.rbt, today)
        with mock.patch("apps.supervision.compliance.organization_today", return_value=today):
            self.assertEqual(reminders.shortfalls(future), {})
            self.assertNotIn("service_hours", reminders.shortfalls(current))
            reminders.refresh(future)
        self.assertFalse(ComplianceReminder.objects.filter(cycle=future).exists())

    def test_rating_scale_uses_the_agreed_names_and_keeps_the_recorded_name(self):
        plan = create_development_plan(organization=self.rbt.organization, relationship=self.rbt,
                                       competencies=[{"name": "Data collection", "current_level": "developing", "target_level": "independent"}])
        competency = plan.competencies.get()
        old = CompetencyAssessment.objects.create(
            organization=self.rbt.organization, relationship=self.rbt, assessed_on=date(2026, 10, 1), rating="developing",
            status=AssessmentStatus.OFFICIAL, plan_competency=competency, competency_name=competency.name,
            documented_content="x", assessed_by=self.jordan)
        CompetencyAssessment.objects.filter(pk=old.pk).update(created_at=timezone.make_aware(timezone.datetime(2026, 10, 1)))
        old.refresh_from_db()
        data = serialize_assessment(old)
        self.assertEqual((data["rating"], data["rating_label"], data["recorded_as"]), ("developing", "Emerging", "Developing"))
        new = CompetencyAssessment.objects.create(
            organization=self.rbt.organization, relationship=self.rbt, assessed_on=date(2026, 11, 20), rating="independent",
            status=AssessmentStatus.OFFICIAL, plan_competency=competency, competency_name=competency.name,
            documented_content="x", assessed_by=self.jordan)
        CompetencyAssessment.objects.filter(pk=new.pk).update(created_at=timezone.make_aware(timezone.datetime(2026, 11, 20)))
        new.refresh_from_db()
        self.assertEqual((serialize_assessment(new)["rating_label"], serialize_assessment(new)["recorded_as"]), ("Proficient", None))

    def test_quick_log_style_record_and_full_record_are_the_same_event(self):
        jordan = self.as_user("jordan@akevra.test")
        # An older completed record without times (as Quick Log used to save)...
        SupervisionSession.objects.create(
            organization=self.rbt.organization, cycle=cycle_for_date(self.rbt, date(2026, 10, 14)), occurred_on=date(2026, 10, 14),
            duration_minutes=60, session_type="individual", documented_by=self.jordan, status="completed")
        # ...blocks documenting the same day's session again
        again = self.post_session(jordan, meeting_method="in_person", topics="Reviewed BIP")
        self.assertEqual(again.status_code, 400)
        self.assertIn("already documented", str(again.data))

    def test_migration_archives_the_incomplete_duplicate_and_keeps_the_documented_record(self):
        import importlib

        from django.apps import apps as django_apps
        from django.db import connection

        cycle = cycle_for_date(self.rbt, date(2026, 10, 14))
        common = dict(organization=self.rbt.organization, cycle=cycle, occurred_on=date(2026, 10, 14), duration_minutes=60,
                      session_type="individual", documented_by=self.jordan, status="completed")
        bare = SupervisionSession.objects.create(**common)
        full = SupervisionSession.objects.create(**common, start_time="10:00", end_time="11:00", meeting_method="in_person", topics="M5 QA")
        other_day = SupervisionSession.objects.create(**{**common, "occurred_on": date(2026, 10, 15)})
        migration = importlib.import_module("apps.supervision.migrations.0029_archive_quick_log_duplicates")
        with connection.schema_editor(atomic=False) as editor:
            migration.forwards(django_apps, editor)
        for row in (bare, full, other_day):
            row.refresh_from_db()
        self.assertEqual((bare.record_status, full.record_status, other_day.record_status), ("archived", "active", "active"))
