"""QA round 5 (M6/M7 acceptance findings): BCaBA two-week periods, fieldwork standards while undetermined,
archived records for audit, plan version comparison and scheduling duplicates."""

from datetime import date
from unittest import mock

from django.core.management import call_command
from django.test import TestCase
from rest_framework.test import APIClient

from apps.accounts.models import UserAccount
from apps.core.tests import PASSWORD
from apps.supervision.compliance import cycle_compliance
from apps.supervision.models import SupervisionSession, SupervisionTrack, SupervisoryRelationship
from apps.supervision.monthly import cycle_for_date

LATER = date(2026, 12, 31)


class QARound5Tests(TestCase):
    @classmethod
    def setUpTestData(cls):
        call_command("seed_sprint0", verbosity=0)
        cls.jordan = UserAccount.objects.get(email="jordan@akevra.test")
        cls.alex = UserAccount.objects.get(email="alex@akevra.test")
        cls.casey = UserAccount.objects.get(email="casey@akevra.test")
        cls.rbt = SupervisoryRelationship.objects.get(supervisor=cls.jordan, supervisee=cls.alex)

    def as_user(self, email):
        client = APIClient()
        token = client.post("/api/v1/auth/login", {"email": email, "password": PASSWORD}, format="json").data["token"]
        client.credentials(HTTP_AUTHORIZATION=f"Bearer {token}")
        return client

    # --- BCaBA: 1 hour in every 2-week period, each period checked on its own --------------------
    def bcaba(self, started_on):
        return SupervisoryRelationship.objects.create(
            organization=self.jordan.organization, supervisor=self.jordan, supervisee=self.casey,
            supervision_track=SupervisionTrack.BCABA_ONGOING, started_on=started_on,
        )

    def session(self, rel, day, minutes):
        return SupervisionSession.objects.create(
            organization=rel.organization, cycle=cycle_for_date(rel, day), occurred_on=day, duration_minutes=minutes,
            session_type="individual", meeting_method="in_person", topics="Supervision", documented_by=self.jordan,
            status="completed",
        )

    def biweekly(self, rel, year, month):
        casey = self.as_user("casey@akevra.test")
        casey.put(f"/api/v1/relationships/{rel.id}/service-hours", {"year": year, "month": month, "hours": "100"}, format="json")
        with mock.patch("apps.supervision.compliance.organization_today", return_value=LATER):
            result = cycle_compliance(cycle_for_date(rel, date(year, month, 1)))
        return next(c for c in result["checks"] if c["key"] == "biweekly_minutes")

    def test_mid_month_start_periods_are_counted_from_the_start_date(self):
        rel = self.bcaba(date(2026, 10, 15))
        self.session(rel, date(2026, 10, 15), 60)
        october = self.biweekly(rel, 2026, 10)
        self.assertEqual([(i["start"], i["end"], i["minutes"], i["status"]) for i in october["intervals"]], [
            ("2026-10-15", "2026-10-28", 60, "met"),
            ("2026-10-29", "2026-11-11", 0, "judged_next_month"),   # runs into November, judged there
        ])
        self.assertEqual((october["status"], october["unit"]), ("met", "minutes"))
        november = self.biweekly(rel, 2026, 11)
        self.assertEqual([(i["start"], i["status"]) for i in november["intervals"]], [
            ("2026-10-29", "not_met"), ("2026-11-12", "not_met"), ("2026-11-26", "judged_next_month"),
        ])
        self.assertEqual(november["status"], "not_met")

    def test_a_full_month_with_supervision_in_only_one_period_is_below(self):
        rel = self.bcaba(date(2026, 9, 1))
        # 120 minutes in October: enough for two periods by monthly total, but all of it in one period
        self.session(rel, date(2026, 10, 5), 120)
        check = self.biweekly(rel, 2026, 10)
        self.assertEqual([(i["start"], i["end"], i["minutes"], i["status"]) for i in check["intervals"]], [
            ("2026-09-29", "2026-10-12", 120, "met"),
            ("2026-10-13", "2026-10-26", 0, "not_met"),
            ("2026-10-27", "2026-11-09", 0, "judged_next_month"),
        ])
        self.assertEqual((check["status"], check["met"], check["actual"]), ("not_met", False, 0))

    def test_a_session_in_the_previous_month_counts_for_the_period_that_spans_both(self):
        rel = self.bcaba(date(2026, 9, 1))
        for day in (date(2026, 9, 30), date(2026, 10, 20), date(2026, 11, 2)):
            self.session(rel, day, 60)
        check = self.biweekly(rel, 2026, 10)
        self.assertEqual([i["status"] for i in check["intervals"]], ["met", "met", "met"])
        self.assertEqual(check["status"], "met")

    # --- Fieldwork standard undetermined: nothing is excluded for good ----------------------------
    def test_undetermined_standard_keeps_each_standards_hours_and_uses_the_chosen_one_later(self):
        rel = SupervisoryRelationship.objects.create(
            organization=self.jordan.organization, supervisor=self.jordan, supervisee=self.casey,
            supervision_track=SupervisionTrack.SUPERVISED_FIELDWORK, fieldwork_subtype="supervised",
            started_on=date(2026, 9, 1), fieldwork_requirements="both",
        )
        casey, jordan = self.as_user("casey@akevra.test"), self.as_user("jordan@akevra.test")
        url = f"/api/v1/relationships/{rel.id}/hours"
        rows = [{"occurred_on": f"2026-09-{d:02d}", "hours": "14", "kind": "independent"} for d in range(1, 11)]
        rows += [{"occurred_on": f"2026-09-{d:02d}", "hours": "2", "kind": "supervision", "supervision_format": "individual",
                  "client_observation": d == 15, **({"observation_minutes": 60} if d == 15 else {})} for d in (15, 18, 22, 25)]
        for row in rows:
            self.assertEqual(casey.post(url, row, format="json").status_code, 201)
        for e in casey.get(url).data["entries"]:
            jordan.post(f"/api/v1/hours/{e['id']}/verify")

        data = jordan.get(f"/api/v1/relationships/{rel.id}/compliance").data
        september = data["months"][-1]
        by_std = september["eligible_by_standard"]
        # 148 verified hours: above the 2022 monthly maximum (130) but within the 2027 one (160)
        self.assertEqual((by_std["bacb_2022"], by_std["bacb_2027"]), (130.0, 148.0))
        self.assertEqual((september["eligible_hours"], september["pending_standard_hours"], september["provisional"]), (130.0, 18.0, True))
        cumulative = data["cumulative"]
        self.assertEqual((cumulative["by_standard"]["bacb_2027"]["supervised_hours"], cumulative["pending_standard_hours"]), (148.0, 18.0))

        # Verified while undetermined, then the standard is set: the stored month uses that standard's own figure
        cycle_id = september["cycle_id"]
        self.assertEqual(jordan.post(f"/api/v1/cycles/{cycle_id}/status", {"status": "closed"}, format="json").status_code, 200)
        pinned = jordan.patch(f"/api/v1/relationships/{rel.id}", {"fieldwork_requirements": "bacb_2027", "fieldwork_standard_basis": "application_year"}, format="json")
        self.assertEqual(pinned.status_code, 200, pinned.data)
        data = jordan.get(f"/api/v1/relationships/{rel.id}/compliance").data
        self.assertEqual((data["months"][-1]["eligible_hours"], data["months"][-1]["standard"]), (148.0, "bacb_2027"))
        self.assertEqual(data["cumulative"]["supervised_hours"], 148.0)
        jordan.patch(f"/api/v1/relationships/{rel.id}", {"fieldwork_requirements": "bacb_2022", "fieldwork_standard_basis": "application_year"}, format="json")
        self.assertEqual(jordan.get(f"/api/v1/relationships/{rel.id}/compliance").data["cumulative"]["supervised_hours"], 130.0)

    # --- Archived records stay retrievable for audit and never count ------------------------------
    def test_archived_session_is_listed_for_audit_with_its_history_and_never_counts(self):
        from apps.audit.models import AuditEvent

        cycle = cycle_for_date(self.rbt, date(2026, 10, 14))
        bare = SupervisionSession.objects.create(
            organization=self.rbt.organization, cycle=cycle, occurred_on=date(2026, 10, 14), duration_minutes=60,
            session_type="individual", documented_by=self.jordan, status="completed", record_status="archived")
        AuditEvent.objects.create(organization_id=self.rbt.organization_id, action="session.archive_duplicate",
                                  entity_type="supervision_session", entity_id=str(bare.id), metadata={"source": "migration 0029"})
        jordan, alex = self.as_user("jordan@akevra.test"), self.as_user("alex@akevra.test")
        url = f"/api/v1/relationships/{self.rbt.id}/sessions"
        default = jordan.get(url).data
        self.assertNotIn(str(bare.id), [s["id"] for s in default["sessions"]])
        self.assertEqual(default["archived_count"], 1)
        listed = {s["id"]: s for s in jordan.get(url + "?archived=1").data["sessions"]}
        row = listed[str(bare.id)]
        self.assertEqual((row["record_status"], row["qualifies"], row["review_flags"]), ("archived", False, []))
        trail = jordan.get(f"/api/v1/audit/trail?entity_type=supervision_session&entity_id={bare.id}")
        self.assertEqual(trail.status_code, 200)
        self.assertIn("session.archive_duplicate", [e["action"] for e in trail.data["events"]])
        # The supervisee doesn't get archived records; nothing about the record changed
        self.assertNotIn(str(bare.id), [s["id"] for s in alex.get(url + "?archived=1").data["sessions"]])
        bare.refresh_from_db()
        self.assertEqual(bare.record_status, "archived")
        with mock.patch("apps.supervision.compliance.organization_today", return_value=LATER):
            october = cycle_compliance(cycle)
        self.assertEqual(october["supervision_hours"], 0.0)

    # --- Scheduling: no duplicates, cancelled appointments keep history and never count -----------
    def test_overlapping_booking_is_refused_and_existing_duplicates_are_archived(self):
        import importlib

        from django.apps import apps as django_apps
        from django.db import connection
        from django.utils.dateparse import parse_datetime

        from apps.supervision.models import Appointment

        jordan = self.as_user("jordan@akevra.test")
        url = f"/api/v1/relationships/{self.rbt.id}/appointments"
        body = {"starts_at": "2026-12-09T16:00:00Z", "ends_at": "2026-12-09T17:00:00Z", "session_type": "individual",
                "meeting_method": "remote"}
        series = jordan.post(url, {**body, "repeat_weeks": 2}, format="json")
        self.assertEqual(series.status_code, 201, series.data)
        again = jordan.post(url, body, format="json")
        self.assertEqual(again.status_code, 400)
        self.assertIn("conflicts", str(again.data))
        # A duplicate saved before bookings were serialized is archived; the series appointment is kept
        first = Appointment.objects.get(pk=series.data["appointments"][0]["id"])
        copy = Appointment.objects.create(organization=first.organization, relationship=self.rbt, starts_at=parse_datetime(body["starts_at"]),
                                          ends_at=parse_datetime(body["ends_at"]), session_type="individual", created_by=self.jordan)
        migration = importlib.import_module("apps.supervision.migrations.0031_archive_duplicate_appointments")
        with connection.schema_editor(atomic=False) as editor:
            migration.forwards(django_apps, editor)
        first.refresh_from_db(); copy.refresh_from_db()
        self.assertEqual((first.record_status, copy.record_status), ("active", "archived"))

    def test_cancelled_appointment_keeps_history_gets_no_reminder_and_no_credit(self):
        from datetime import timedelta

        from django.utils import timezone

        from apps.supervision.models import Notification
        from apps.supervision.notifications import send_preparation_reminders

        jordan = self.as_user("jordan@akevra.test")
        start = timezone.now() + timedelta(hours=20)
        made = jordan.post(f"/api/v1/relationships/{self.rbt.id}/appointments",
                           {"starts_at": start.isoformat(), "ends_at": (start + timedelta(hours=1)).isoformat(),
                            "session_type": "individual", "meeting_method": "remote"}, format="json")
        appt_id = made.data["appointments"][0]["id"]
        self.assertEqual(jordan.post(f"/api/v1/appointments/{appt_id}/cancel", {"reason": "Supervisee ill"}, format="json").status_code, 200)
        send_preparation_reminders()
        self.assertFalse(Notification.objects.filter(appointment_id=appt_id, kind="preparation_reminder").exists())
        detail = jordan.get(f"/api/v1/appointments/{appt_id}").data
        self.assertEqual(detail["status"], "cancelled")
        self.assertGreaterEqual(len(detail.get("events") or detail.get("history") or []), 2)
        doc = jordan.post(f"/api/v1/relationships/{self.rbt.id}/sessions",
                          {"occurred_on": "2026-10-20", "start_time": "10:00", "end_time": "11:00", "session_type": "individual",
                           "meeting_method": "remote", "topics": "x", "appointment_id": appt_id}, format="json")
        self.assertEqual(doc.status_code, 400)
        self.assertIn("cancelled", str(doc.data))

    # --- Effective-dated compliance rules: each month uses the rule set in force for it ------------
    def test_each_month_uses_the_rule_set_effective_for_it(self):
        alex = self.as_user("alex@akevra.test")
        jordan = self.as_user("jordan@akevra.test")
        for month in (10, 11):
            alex.put(f"/api/v1/relationships/{self.rbt.id}/service-hours", {"year": 2026, "month": month, "hours": "100"}, format="json")
        call_command("approve_compliance_rules", name="Set A", effective_from="2026-01-01", track=["rbt_ongoing_supervision"], verbosity=0)
        call_command("approve_compliance_rules", name="Set B", effective_from="2026-11-01", track=["rbt_ongoing_supervision"],
                     set=["rbt_ongoing_supervision:min_supervision_percent=7"], verbosity=0)
        months = {(m["year"], m["month"]): m for m in jordan.get(f"/api/v1/relationships/{self.rbt.id}/compliance").data["months"]}
        october, november = months[(2026, 10)], months[(2026, 11)]
        self.assertEqual((october["rules"]["name"], october["required_supervision_percent"]), ("Set A", 5.0))
        self.assertEqual((november["rules"]["name"], november["required_supervision_percent"]), ("Set B", 7.0))

    def test_a_month_verified_before_period_detail_shows_its_periods_and_keeps_its_result(self):
        from apps.supervision.models import MonthlyCycle

        rel = self.bcaba(date(2026, 10, 15))
        self.session(rel, date(2026, 10, 15), 60)
        self.biweekly(rel, 2026, 10)
        jordan = self.as_user("jordan@akevra.test")
        cycle = cycle_for_date(rel, date(2026, 10, 1))
        with mock.patch("apps.supervision.compliance.organization_today", return_value=LATER):
            self.assertEqual(jordan.post(f"/api/v1/cycles/{cycle.id}/status", {"status": "closed"}, format="json").status_code, 200)
        # As stored by the earlier half-month check (no period dates)
        cycle.refresh_from_db()
        snap = cycle.closed_snapshot
        for check in snap["result"]["checks"]:
            if check["key"] == "biweekly_minutes":
                check["intervals"] = [{"half": 2, "minutes": 60, "status": "met"}]
        MonthlyCycle.objects.filter(pk=cycle.pk).update(closed_snapshot=snap)
        cycle.refresh_from_db()
        with mock.patch("apps.supervision.compliance.organization_today", return_value=LATER):
            result = cycle_compliance(cycle)
        self.assertEqual(result["status"], snap["result"]["status"])           # the verified result is kept
        detail = next(c for c in result["recalculation"]["checks"] if c["key"] == "biweekly_minutes")
        self.assertFalse(result["recalculation"]["differs"])
        self.assertEqual([(i["start"], i["status"]) for i in detail["intervals"]],
                         [("2026-10-15", "met"), ("2026-10-29", "judged_next_month")])
