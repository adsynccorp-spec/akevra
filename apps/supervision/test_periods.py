from datetime import date

from django.core.management import call_command
from django.test import TestCase
from rest_framework.test import APIClient

from apps.accounts.models import UserAccount
from apps.core.tests import PASSWORD
from apps.supervision.models import ServiceHoursRecord, SupervisionTrack, SupervisoryRelationship


class ReportingPeriodTests(TestCase):
    """Shared service hours, missing vs confirmed zero, provenance, period status with snapshots,
    concurrent supervisors, Requirements Coordinators and the audit log."""

    @classmethod
    def setUpTestData(cls):
        call_command("seed_sprint0", verbosity=0)
        cls.jordan = UserAccount.objects.get(email="jordan@akevra.test")
        cls.morgan = UserAccount.objects.get(email="morgan@akevra.test")
        cls.alex = UserAccount.objects.get(email="alex@akevra.test")
        cls.rbt = SupervisoryRelationship.objects.get(supervisor=cls.jordan, supervisee=cls.alex)
        # A second, concurrent supervisor for the same RBT
        cls.second = SupervisoryRelationship.objects.create(
            organization=cls.jordan.organization, supervisor=cls.morgan, supervisee=cls.alex,
            supervision_track=SupervisionTrack.RBT_ONGOING, started_on=date(2026, 7, 1),
        )

    def as_user(self, email):
        client = APIClient()
        response = client.post("/api/v1/auth/login", {"email": email, "password": PASSWORD}, format="json")
        client.credentials(HTTP_AUTHORIZATION=f"Bearer {response.data['token']}")
        return client

    def attest(self, client, month, hours, rel=None, **extra):
        return client.put(f"/api/v1/relationships/{(rel or self.rbt).id}/service-hours",
                          {"year": 2026, "month": month, "hours": hours, **extra}, format="json")

    def session(self, client, rel, day, minutes, **extra):
        response = client.post(f"/api/v1/relationships/{rel.id}/sessions",
                               {"occurred_on": day, "duration_minutes": minutes, "session_type": "individual",
                                "meeting_method": "in_person", **extra}, format="json")
        self.assertEqual(response.status_code, 201, response.data)
        return response.data

    def month(self, client, rel, month):
        return next(m for m in client.get(f"/api/v1/relationships/{rel.id}/compliance").data["months"] if m["month"] == month)

    def test_shared_denominator_concurrent_supervisors_and_corrections(self):
        jordan, morgan, alex = self.as_user("jordan@akevra.test"), self.as_user("morgan@akevra.test"), self.as_user("alex@akevra.test")
        self.assertEqual(self.attest(alex, 8, "100").status_code, 200)
        self.session(jordan, self.rbt, "2026-08-04", 60, client_observation=True)
        self.session(morgan, self.second, "2026-08-18", 120)

        # One service-hours record serves both relationships, but each relationship's requirement
        # counts only its own sessions; the supervisee's overall picture shows both supervisors
        mine, theirs = self.month(jordan, self.rbt, 8), self.month(morgan, self.second, 8)
        self.assertEqual((mine["service_hours"], mine["supervision_hours"], mine["supervision_percent"]), (100.0, 1.0, 1.0))
        self.assertEqual((theirs["service_hours"], theirs["supervision_hours"], theirs["supervision_percent"]), (100.0, 2.0, 2.0))
        self.assertEqual(({c["key"]: c["actual"] for c in theirs["checks"]}["observation_contacts"]), 0)
        for result in (mine, theirs):
            self.assertEqual((result["overall"]["supervision_hours"], result["overall"]["internal_supervision_hours"]), (3.0, 3.0))
        self.assertEqual(sorted(c["supervision_hours"] for c in mine["contributions"]), [1.0, 2.0])
        self.assertEqual([c["this_relationship"] for c in mine["contributions"] if c["supervision_hours"] == 1.0], [True])
        self.assertEqual(ServiceHoursRecord.objects.filter(supervisee=self.alex, year=2026, month=8, record_status="active").count(), 1)

        # Attesting through the other relationship corrects the same record; the original is kept
        self.assertEqual(self.attest(alex, 8, "120", rel=self.second, supporting_reference="Timesheet #42").status_code, 200)
        history = jordan.get(f"/api/v1/supervisees/{self.alex.id}/service-hours").data["records"]
        august = [r for r in history if r["month"] == 8]
        self.assertEqual([(r["hours"], r["status"]) for r in august], [(120.0, "active"), (100.0, "superseded")])
        self.assertEqual(august[0]["supersedes_id"], august[1]["id"])
        self.assertEqual((august[0]["source_type"], august[0]["supporting_reference"], august[0]["entered_by"]["id"]),
                         ("self_attested", "Timesheet #42", str(self.alex.id)))
        # A supervisor reviews it; the supervisee can't
        self.assertEqual(alex.post(f"/api/v1/service-hours/{august[0]['id']}/review").status_code, 403)
        self.assertIsNotNone(jordan.post(f"/api/v1/service-hours/{august[0]['id']}/review").data["reviewed_at"])

    def test_missing_versus_confirmed_zero(self):
        jordan, alex = self.as_user("jordan@akevra.test"), self.as_user("alex@akevra.test")
        self.session(jordan, self.rbt, "2026-07-08", 60)
        self.assertEqual(self.month(jordan, self.rbt, 7)["status"], "awaiting_service_hours")
        refused = self.attest(alex, 7, "0")
        self.assertEqual(refused.status_code, 400)
        self.assertIn("reason", str(refused.data))
        self.assertEqual(self.attest(alex, 7, "0", zero_reason="service_hold").status_code, 200)
        july = self.month(jordan, self.rbt, 7)
        self.assertEqual((july["status"], july["service_hours_record"]["zero_reason"]), ("no_service_hours", "service_hold"))

    def test_period_status_snapshot_and_reopen(self):
        jordan, alex = self.as_user("jordan@akevra.test"), self.as_user("alex@akevra.test")
        self.attest(alex, 6, "40")
        self.session(jordan, self.rbt, "2026-06-10", 120, client_observation=True)
        cycle = next(c for c in jordan.get(f"/api/v1/relationships/{self.rbt.id}/cycles").data["cycles"] if c["month"] == 6)
        self.assertEqual(cycle["status"], "open")

        current = next(c for c in jordan.get(f"/api/v1/relationships/{self.rbt.id}/cycles").data["cycles"] if c["month"] == 6)
        self.assertEqual(alex.post(f"/api/v1/cycles/{current['id']}/status", {"status": "ready_for_review"}, format="json").status_code, 403)
        self.assertEqual(jordan.post(f"/api/v1/cycles/{cycle['id']}/status", {"status": "ready_for_review"}, format="json").data["status"], "ready_for_review")
        closed = jordan.post(f"/api/v1/cycles/{cycle['id']}/status", {"status": "closed"}, format="json").data
        self.assertEqual(closed["status"], "closed")
        self.assertIn("snapshot", closed["compliance"])
        self.assertEqual(closed["compliance"]["supervision_hours"], 2.0)

        # Closed: compliance-bearing changes are blocked and the stored snapshot is reported
        self.assertEqual(jordan.post(f"/api/v1/relationships/{self.rbt.id}/sessions",
                                     {"occurred_on": "2026-06-20", "duration_minutes": 60, "session_type": "individual"}, format="json").status_code, 400)
        blocked = self.attest(alex, 6, "50")
        self.assertEqual(blocked.status_code, 400)
        self.assertIn("closed", str(blocked.data))
        self.assertIn("snapshot", self.month(jordan, self.rbt, 6))

        # Reopening needs a reason; the history keeps every change
        self.assertEqual(jordan.post(f"/api/v1/cycles/{cycle['id']}/status", {"status": "open"}, format="json").status_code, 400)
        reopened = jordan.post(f"/api/v1/cycles/{cycle['id']}/status", {"status": "open", "reason": "Late timesheet correction"}, format="json").data
        self.assertEqual(reopened["status"], "open")
        self.assertEqual([e["to_status"] for e in reopened["status_history"]], ["ready_for_review", "closed", "open"])
        self.assertEqual(reopened["status_history"][-1]["reason"], "Late timesheet correction")
        self.assertEqual(self.attest(alex, 6, "50").status_code, 200)

        # The current month can't be closed
        this_month = jordan.post(f"/api/v1/relationships/{self.rbt.id}/cycles", {"year": date.today().year, "month": date.today().month}, format="json")
        current_id = this_month.data.get("id") or next(c["id"] for c in jordan.get(f"/api/v1/relationships/{self.rbt.id}/cycles").data["cycles"]
                                                       if (c["year"], c["month"]) == (date.today().year, date.today().month))
        self.assertEqual(jordan.post(f"/api/v1/cycles/{current_id}/status", {"status": "closed"}, format="json").status_code, 400)

    def test_coordinator_certification_and_audit_log(self):
        jordan, morgan, alex = self.as_user("jordan@akevra.test"), self.as_user("morgan@akevra.test"), self.as_user("alex@akevra.test")
        casey = self.as_user("casey@akevra.test")
        self.attest(alex, 8, "120")
        self.session(jordan, self.rbt, "2026-08-04", 210, client_observation=True)
        jordan.post(f"/api/v1/relationships/{self.rbt.id}/sessions",
                    {"occurred_on": "2026-08-20", "duration_minutes": 75, "session_type": "group", "meeting_method": "remote"}, format="json")
        self.session(morgan, self.second, "2026-08-25", 30)

        self.assertEqual(alex.patch(f"/api/v1/people/{self.jordan.id}/certification", {"certification_number": "1-01-11007"}, format="json").status_code, 403)
        self.assertEqual(jordan.patch(f"/api/v1/people/{self.jordan.id}/certification", {"certification_number": "1-01-11007"}, format="json").status_code, 200)
        designated = jordan.post(f"/api/v1/supervisees/{self.alex.id}/coordinators",
                                 {"coordinator_id": str(self.morgan.id), "starts_on": "2026-08-01"}, format="json")
        self.assertEqual(designated.status_code, 201, designated.data)
        self.assertEqual(alex.post(f"/api/v1/supervisees/{self.alex.id}/coordinators",
                                   {"coordinator_id": str(self.morgan.id), "starts_on": "2026-08-01"}, format="json").status_code, 403)

        log = jordan.get(f"/api/v1/supervisees/{self.alex.id}/audit-log?start=2026-07&months=3").data
        july, august, september = log["rows"]
        self.assertEqual((august["service_hours"], august["individual_supervision_hours"], august["group_supervision_hours"],
                          august["observations_with_client"], august["face_to_face_contacts"]), (120.0, 4.0, 1.25, 1, 3))
        self.assertIn("Jordan", august["people_label"])
        self.assertIn("#1-01-11007", august["people_label"])
        self.assertIn("RBT Requirements Coordinator", [p["role"] for p in august["people"]])
        self.assertEqual(len(august["trace"]["sessions"]), 3)
        self.assertIn("missing_service_hours", [f["code"] for f in july["flags"]])
        self.assertIn("missing_certification", [f["code"] for f in august["flags"]])  # Morgan has no number yet

        csv = jordan.get(f"/api/v1/supervisees/{self.alex.id}/audit-log?start=2026-07&months=3&export=csv")
        self.assertEqual(csv["Content-Type"], "text/csv")
        lines = csv.content.decode().splitlines()
        self.assertTrue(lines[0].startswith("Month,Year,Total # of RBT Behavior-Analytic Hours Worked"))
        self.assertTrue(lines[2].startswith("August,2026,120.00,4.00,1.25,1,3,"))

        # The supervisee can see their own log; an unrelated person can't
        self.assertEqual(alex.get(f"/api/v1/supervisees/{self.alex.id}/audit-log").status_code, 200)
        self.assertEqual(casey.get(f"/api/v1/supervisees/{self.alex.id}/audit-log").status_code, 403)

    def test_qa_round3_corrections_future_sessions_and_agency_lists(self):
        """Client QA round 3: an unchanged correction doesn't reset reviews, future-dated sessions
        can't be completed or counted early, and a supervision-only agency isn't listed as a
        service-hours contributor."""
        from datetime import timedelta

        from django.utils import timezone

        from apps.supervision.models import SupervisionSession

        jordan, alex = self.as_user("jordan@akevra.test"), self.as_user("alex@akevra.test")
        record = self.attest(alex, 8, "105", notes="Timesheet").data
        jordan.post(f"/api/v1/service-hours/{record['id']}/review", {"relationship_id": str(self.rbt.id)}, format="json")
        unchanged = self.attest(alex, 8, "105", notes="Timesheet")
        self.assertEqual(unchanged.status_code, 400)
        self.assertIn("Nothing has changed", str(unchanged.data))
        current = self.month(jordan, self.rbt, 8)
        records = jordan.get(f"/api/v1/supervisees/{self.alex.id}/service-hours").data["records"]
        self.assertEqual([(r["id"], len(r["reviews"])) for r in records if r["status"] == "active"], [(record["id"], 1)])
        # A real change (notes included) is still a correction that needs a new review
        changed = self.attest(alex, 8, "105", notes="Timesheet v2").data
        self.assertEqual((changed["supersedes_id"], changed["reviews"]), (record["id"], []))
        self.assertEqual(current["service_hours"], 105.0)

        # Future-dated sessions
        tomorrow = (timezone.localdate() + timedelta(days=2)).isoformat()
        refused = jordan.post(f"/api/v1/relationships/{self.rbt.id}/sessions",
                              {"occurred_on": tomorrow, "duration_minutes": 60, "session_type": "individual", "meeting_method": "in_person"},
                              format="json")
        self.assertEqual(refused.status_code, 400)
        self.assertIn("hasn't happened yet", str(refused.data))
        draft = jordan.post(f"/api/v1/relationships/{self.rbt.id}/sessions",
                            {"occurred_on": tomorrow, "duration_minutes": 60, "session_type": "individual", "meeting_method": "in_person",
                             "draft": True}, format="json")
        self.assertEqual(draft.status_code, 201, draft.data)
        # A future-dated completed record (e.g. imported earlier) doesn't count before its date
        future = date.fromisoformat(tomorrow)
        SupervisionSession.objects.filter(pk=draft.data["id"]).update(status="completed")
        month = self.month(jordan, self.rbt, future.month)
        self.assertEqual(month["supervision_hours"], 0.0)

        # A supervision-only agency isn't a service-hours contributor
        self.attest(alex, 8, "20", scope="external", external_organization="Bright Path ABA")
        alex.post(f"/api/v1/supervisees/{self.alex.id}/external-supervision",
                  {"occurred_on": "2026-08-12", "duration_minutes": 60, "supervisor_name": "Dana Lee", "organization_name": "Harbor ABA"},
                  format="json")
        overall = self.month(jordan, self.rbt, 8)["overall"]
        self.assertEqual(overall["external_service_agencies"], ["Bright Path ABA"])
        self.assertEqual(overall["external_supervision_agencies"], ["Harbor ABA"])

    def test_internal_and_external_sources(self):
        """Another agency's hours and supervision are kept, attributed to their source, in one
        supervisee-month picture — without changing what this organization is responsible for."""
        jordan, alex, casey = self.as_user("jordan@akevra.test"), self.as_user("alex@akevra.test"), self.as_user("casey@akevra.test")
        self.attest(alex, 8, "100")
        self.session(jordan, self.rbt, "2026-08-05", 300, client_observation=True)
        refused = self.attest(alex, 8, "40", scope="external")
        self.assertEqual(refused.status_code, 400)
        self.assertIn("agency", str(refused.data))
        self.assertEqual(self.attest(alex, 8, "40", scope="external", external_organization="Bright Path ABA").status_code, 200)
        url = f"/api/v1/supervisees/{self.alex.id}/external-supervision"
        external = {"occurred_on": "2026-08-12", "duration_minutes": 60, "supervisor_name": "Dana Lee",
                    "supervisor_certification_number": "1-22-33333", "organization_name": "Bright Path ABA"}
        self.assertEqual(casey.post(url, external, format="json").status_code, 403)
        created = alex.post(url, external, format="json")
        self.assertEqual(created.status_code, 201, created.data)
        self.assertEqual(created.data["source_type"], "self_attested")

        month = self.month(jordan, self.rbt, 8)
        # This organization's requirement: internal hours and internal supervision only
        self.assertEqual((month["scope"], month["service_hours"], month["supervision_hours"], month["supervision_percent"]),
                         ("organization", 100.0, 5.0, 5.0))
        # The supervisee's overall picture includes the other agency, attributed to it
        overall = month["overall"]
        self.assertEqual((overall["service_hours"], overall["internal_service_hours"], overall["external_service_hours"]), (140.0, 100.0, 40.0))
        self.assertEqual((overall["supervision_hours"], overall["external_supervision_hours"]), (6.0, 1.0))
        self.assertEqual(overall["external_agencies"], ["Bright Path ABA"])
        self.assertIn({"supervisor": "Dana Lee", "organization": "Bright Path ABA", "source": "external"},
                      [{k: c.get(k) for k in ("supervisor", "organization", "source")} for c in month["contributions"]])
        # Only one internal record exists for the month; the external one is separate
        records = jordan.get(f"/api/v1/supervisees/{self.alex.id}/service-hours").data["records"]
        self.assertEqual(sorted((r["scope"], r["hours"]) for r in records if r["month"] == 8 and r["status"] == "active"),
                         [("external", 40.0), ("internal", 100.0)])

        august = jordan.get(f"/api/v1/supervisees/{self.alex.id}/audit-log?start=2026-08&months=1").data["rows"][0]
        self.assertEqual((august["service_hours"], august["internal_service_hours"], august["external_service_hours"]), (140.0, 100.0, 40.0))
        self.assertEqual((august["individual_supervision_hours"], august["face_to_face_contacts"]), (6.0, 2))
        self.assertIn("Dana Lee #1-22-33333", august["people_label"])
        self.assertIn("Bright Path ABA", august["organizations"])

    def test_supervisee_sees_and_exports_only_their_own_audit_log(self):
        alex, casey = self.as_user("alex@akevra.test"), self.as_user("casey@akevra.test")
        self.attest(alex, 8, "90")
        people = alex.get("/api/v1/audit/people").data
        self.assertEqual([p["id"] for p in people["supervisees"]], [str(self.alex.id)])
        self.assertEqual((people["own_only"], people["can_manage"], people["directory"]), (True, False, []))
        own = alex.get(f"/api/v1/supervisees/{self.alex.id}/audit-log?start=2026-08&months=1&export=csv")
        self.assertEqual(own.status_code, 200)
        self.assertIn("August,2026,90.00", own.content.decode())
        # Read-only: no coordinator changes, nobody else's log
        self.assertFalse(alex.get(f"/api/v1/supervisees/{self.alex.id}/coordinators").data["can_manage"])
        self.assertEqual(alex.get(f"/api/v1/supervisees/{UserAccount.objects.get(email='casey@akevra.test').id}/audit-log").status_code, 403)
        self.assertEqual(casey.get(f"/api/v1/supervisees/{self.alex.id}/audit-log?export=csv").status_code, 403)
