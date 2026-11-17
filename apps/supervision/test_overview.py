from datetime import date

from django.core.management import call_command
from django.test import TestCase
from rest_framework.test import APIClient

from apps.accounts.models import UserAccount
from apps.core.tests import PASSWORD
from apps.supervision.models import SupervisoryRelationship
from apps.supervision.services import create_development_plan


def _months_ago(n):
    today = date.today()
    index = today.year * 12 + today.month - 1 - n
    return index // 12, index % 12 + 1


class OverviewTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        call_command("seed_sprint0", verbosity=0)
        cls.jordan = UserAccount.objects.get(email="jordan@akevra.test")
        cls.alex = UserAccount.objects.get(email="alex@akevra.test")
        cls.rbt = SupervisoryRelationship.objects.get(supervisor=cls.jordan, supervisee=cls.alex)
        cls.plan = create_development_plan(
            organization=cls.rbt.organization, relationship=cls.rbt,
            competencies=[{"name": "Data collection"}, {"name": "Session documentation"}, {"name": "Ethics"}],
        )

    def as_user(self, email):
        client = APIClient()
        token = client.post("/api/v1/auth/login", {"email": email, "password": PASSWORD}, format="json").data["token"]
        client.credentials(HTTP_AUTHORIZATION=f"Bearer {token}")
        return client

    def _row(self, data):
        return next((r for r in data["relationships"] if r["relationship"]["id"] == str(self.rbt.id)), None)

    def _month(self, client, months_ago, hours, sessions):
        year, month = _months_ago(months_ago)
        for day, minutes, observation in sessions:
            response = client["jordan"].post(
                f"/api/v1/relationships/{self.rbt.id}/sessions",
                {"occurred_on": f"{year}-{month:02d}-{day:02d}", "duration_minutes": minutes, "session_type": "individual",
                 "client_observation": observation, "meeting_method": "in_person", "topics": "Supervision review"}, format="json")
            self.assertEqual(response.status_code, 201, response.data)
        attested = client["alex"].put(f"/api/v1/relationships/{self.rbt.id}/service-hours",
                                      {"year": year, "month": month, "hours": str(hours)}, format="json")
        self.assertEqual(attested.status_code, 200, attested.data)

    def test_compliance_overview_status_and_scoping(self):
        client = {"jordan": self.as_user("jordan@akevra.test"), "alex": self.as_user("alex@akevra.test")}
        url = "/api/v1/compliance/overview"
        empty = self._row(client["jordan"].get(url).data)
        self.assertEqual(empty["status"], "no_data")

        # Two months ago: 4 h of supervision on 80 service hours = 5 % -> compliant
        self._month(client, 2, 80, [(3, 180, True), (10, 60, False)])
        row = self._row(client["jordan"].get(url).data)
        self.assertEqual((row["status"], row["current_percent"], row["required_percent"], row["score"]), ("compliant", 5.0, 5.0, 100.0))

        # Last month (ended): 1 h on 100 h = 1 % -> at risk, ranked in needs attention
        self._month(client, 1, 100, [(14, 60, True)])
        data = client["jordan"].get(url).data
        row = self._row(data)
        self.assertEqual((row["status"], row["current_percent"], row["score"]), ("at_risk", 1.0, 20.0))
        self.assertEqual(row["remaining_supervision_hours"], 4.0)
        self.assertIn(str(self.rbt.id), data["needs_attention"])
        self.assertEqual(data["summary"]["at_risk"], 1)
        trend = {(t["year"], t["month"]): t for t in data["trend"]}
        self.assertEqual(trend[_months_ago(2)]["percent"], 100.0)
        self.assertEqual(trend[_months_ago(1)]["percent"], 0.0)
        self.assertEqual(len(data["trend"]), 6)

        # The supervisee sees the same figures for their own relationship; others see only what they may
        self.assertEqual(self._row(client["alex"].get(url).data)["status"], "at_risk")
        self.assertEqual(self._row(self.as_user("director@akevra.test").get(url).data)["status"], "at_risk")
        self.assertIsNone(self._row(self.as_user("summit.supervisor@akevra.test").get(url).data))
        self.assertIsNone(self._row(self.as_user("settings.admin@akevra.test").get(url).data))

    def test_competency_overview(self):
        jordan, alex = self.as_user("jordan@akevra.test"), self.as_user("alex@akevra.test")
        data_collection, documentation, _ethics = self.plan.competencies.order_by("sort_order")
        create = f"/api/v1/relationships/{self.rbt.id}/assessments"

        def official(competency, rating, day):
            a = jordan.post(create, {"plan_competency_id": str(competency.id), "rating": rating, "assessed_on": day,
                                     "documented_content": "Observed across sessions."}, format="json").data
            self.assertEqual(jordan.post(f"/api/v1/assessments/{a['id']}/finalize").data["status"], "official")
            return a

        official(data_collection, "independent", "2026-08-01")
        official(data_collection, "developing", "2026-09-01")  # a regression
        official(documentation, "needs_training", "2026-09-02")
        pending = jordan.post(create, {"plan_competency_id": str(documentation.id), "rating": "developing",
                                       "assessed_on": "2026-09-20", "documented_content": "Better."}, format="json").data
        jordan.post(f"/api/v1/assessments/{pending['id']}/submit")
        jordan.post(create, {"plan_competency_id": str(_ethics.id), "assessed_on": "2026-09-21"}, format="json")

        url = "/api/v1/competencies/overview"
        mine = jordan.get(url).data
        self.assertEqual(mine["summary"]["due"], 1)  # Ethics has no official rating yet
        self.assertEqual((mine["summary"]["awaiting_review"], mine["summary"]["in_progress"]), (1, 1))
        self.assertEqual((mine["summary"]["rated"], mine["summary"]["competent"], mine["summary"]["regressions"]), (2, 0, 1))
        self.assertEqual(mine["summary"]["needs_attention"], 2)
        counts = {d["rating"]: d["count"] for d in mine["distribution"]}
        self.assertEqual(counts, {"independent": 0, "performs_with_support": 0, "developing": 1, "needs_training": 1})
        self.assertEqual(sorted(a["kind"] for a in mine["attention"]), ["needs_training", "pending_review", "regression"])
        self.assertEqual(len(mine["assessments"]), 5)

        # The supervisee sees official assessments only; nothing awaiting the supervisor's review
        theirs = alex.get(url).data
        self.assertEqual(len(theirs["assessments"]), 3)
        self.assertTrue(all(a["status"] == "official" for a in theirs["assessments"]))
        self.assertEqual((theirs["summary"]["awaiting_review"], theirs["summary"]["in_progress"]), (0, 0))
        self.assertNotIn("pending_review", [a["kind"] for a in theirs["attention"]])
        self.assertEqual(self.as_user("summit.supervisor@akevra.test").get(url).data["assessments"], [])

    def test_audit_activity_and_sessions(self):
        jordan, alex = self.as_user("jordan@akevra.test"), self.as_user("alex@akevra.test")
        jordan.post(f"/api/v1/relationships/{self.rbt.id}/notes", {"body": "Note"}, format="json")
        own = alex.get("/api/v1/audit/activity").data
        self.assertEqual(own["scope"], "own")
        self.assertTrue(all(e["is_me"] for e in own["events"]))
        self.assertNotIn("note.create", [e["action"] for e in own["events"]])
        self.assertIn("note.create", [e["action"] for e in jordan.get("/api/v1/audit/activity").data["events"]])
        director = self.as_user("director@akevra.test").get("/api/v1/audit/activity").data
        self.assertEqual(director["scope"], "organization")
        self.assertIn("note.create", [e["action"] for e in director["events"]])

        sessions = alex.get("/api/v1/account/sessions").data["sessions"]
        self.assertEqual(sum(s["current"] for s in sessions), 1)
        self.assertEqual(alex.post("/api/v1/account/sessions").status_code, 405)

    def test_my_profile(self):
        profile = self.as_user("jordan@akevra.test").get("/api/v1/account/profile").data
        self.assertEqual(profile["id"], str(self.jordan.id))
        self.assertEqual(profile["caseload"]["by_track"].get(self.rbt.supervision_track), 1)
        self.assertGreaterEqual(profile["supervised_by"], 1)  # Jordan is also Morgan's supervisee
        self.assertEqual(self.as_user("alex@akevra.test").get("/api/v1/account/profile").data["caseload"]["active"], 0)
