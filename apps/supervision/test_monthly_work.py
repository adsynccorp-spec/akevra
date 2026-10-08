import shutil
import tempfile
from datetime import date

from django.core.exceptions import ValidationError
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import SimpleTestCase, TestCase, override_settings
from rest_framework.test import APIClient

from apps.accounts.models import UserAccount
from apps.core.tests import PASSWORD
from apps.supervision.compliance import (
    default_rules,
    evaluate_fieldwork_cumulative,
    evaluate_fieldwork_month,
    evaluate_ongoing_month,
)
from apps.supervision.models import (
    ComplianceRuleParameter,
    ComplianceRuleSet,
    HoursEntry,
    MonthlyCycle,
    SupervisionTrack,
    SupervisoryRelationship,
)

MEDIA = tempfile.mkdtemp(prefix="akevra-test-media-")


@override_settings(MEDIA_ROOT=MEDIA)
class MonthlyWorkAcceptanceTests(TestCase):
    """Maps 1:1 to the Monthly Work & Hours Tracking milestone."""

    @classmethod
    def setUpTestData(cls):
        from django.core.management import call_command

        call_command("seed_sprint0", verbosity=0)
        cls.jordan = UserAccount.objects.get(email="jordan@akevra.test")
        cls.alex = UserAccount.objects.get(email="alex@akevra.test")
        cls.casey = UserAccount.objects.get(email="casey@akevra.test")
        cls.rbt = SupervisoryRelationship.objects.get(supervisor=cls.jordan, supervisee=cls.alex)
        cls.fieldwork = SupervisoryRelationship.objects.create(
            organization=cls.jordan.organization,
            supervisor=cls.jordan,
            supervisee=cls.casey,
            supervision_track=SupervisionTrack.SUPERVISED_FIELDWORK,
            fieldwork_subtype="supervised",
            started_on=date(2026, 6, 1),
        )

    @classmethod
    def tearDownClass(cls):
        super().tearDownClass()
        shutil.rmtree(MEDIA, ignore_errors=True)

    def as_user(self, email):
        client = APIClient()
        response = client.post("/api/v1/auth/login", {"email": email, "password": PASSWORD}, format="json")
        client.credentials(HTTP_AUTHORIZATION=f"Bearer {response.data['token']}")
        self.assertEqual(response.data.get("status"), "authenticated", response.data)
        return client

    def pdf(self, name="evidence.pdf", body=b"%PDF-1.4 test"):
        return SimpleUploadedFile(name, body, content_type="application/pdf")

    def log_hours(self, client, day, hours, kind="independent", **extra):
        response = client.post(
            f"/api/v1/relationships/{self.fieldwork.id}/hours",
            {"occurred_on": day, "hours": hours, "kind": kind, **extra},
            format="json",
        )
        self.assertEqual(response.status_code, 201, response.data)
        return response.data

    # ------------------------------------------------------------------
    # 1. The monthly cycle groups that month's assignments, hours and sessions
    # ------------------------------------------------------------------
    def test_01_cycle_groups_the_months_work(self):
        jordan = self.as_user("jordan@akevra.test")
        alex = self.as_user("alex@akevra.test")
        casey = self.as_user("casey@akevra.test")

        issued = jordan.post(
            f"/api/v1/relationships/{self.rbt.id}/assignments",
            {"title": "Ethics scenario", "due_on": "2026-08-20"},
            format="json",
        )
        self.assertEqual(issued.status_code, 201, issued.data)
        for day in ("2026-08-10", "2026-07-30"):
            documented = jordan.post(
                f"/api/v1/relationships/{self.rbt.id}/sessions",
                {"occurred_on": day, "duration_minutes": 60, "session_type": "individual"},
                format="json",
            )
            self.assertEqual(documented.status_code, 201, documented.data)
        attested = alex.put(
            f"/api/v1/relationships/{self.rbt.id}/service-hours",
            {"year": 2026, "month": 8, "hours": "120"},
            format="json",
        )
        self.assertEqual(attested.status_code, 200, attested.data)
        self.log_hours(casey, "2026-08-05", "6")

        listed = alex.get(f"/api/v1/relationships/{self.rbt.id}/cycles")
        self.assertEqual(listed.status_code, 200)
        cycles = {(c["year"], c["month"]): c for c in listed.data["cycles"]}
        self.assertEqual(set(cycles), {(2026, 7), (2026, 8)})
        august, july = cycles[(2026, 8)], cycles[(2026, 7)]
        self.assertEqual(august["counts"]["assignments"], 1)
        self.assertEqual(august["counts"]["sessions"], 1)
        self.assertEqual(august["service_hours"]["hours"], 120.0)
        self.assertEqual(july["counts"]["assignments"], 0)
        self.assertEqual(july["counts"]["sessions"], 1)
        # Another relationship's hours in the same month stay in that relationship's cycle
        self.assertEqual(august["counts"]["hours_entries"], 0)

        detail = alex.get(f"/api/v1/cycles/{august['id']}")
        self.assertEqual(detail.status_code, 200)
        self.assertEqual([a["title"] for a in detail.data["assignments"]], ["Ethics scenario"])
        self.assertEqual([s["occurred_on"] for s in detail.data["sessions"]], ["2026-08-10"])

        fieldwork_cycles = casey.get(f"/api/v1/relationships/{self.fieldwork.id}/cycles").data["cycles"]
        self.assertEqual([(c["year"], c["month"], c["counts"]["hours_entries"]) for c in fieldwork_cycles], [(2026, 8, 1)])

        duplicate = jordan.post(f"/api/v1/relationships/{self.rbt.id}/cycles", {"year": 2026, "month": 8}, format="json")
        self.assertEqual(duplicate.status_code, 400)
        self.assertEqual(MonthlyCycle.objects.filter(relationship=self.rbt, year=2026, month=8).count(), 1)

        not_party = self.as_user("summit.supervisor@akevra.test")
        self.assertEqual(not_party.get(f"/api/v1/cycles/{august['id']}").status_code, 404)

    # ------------------------------------------------------------------
    # 2. Assignment lifecycle: issue -> submit with file -> revision -> completion
    # ------------------------------------------------------------------
    def test_02_full_assignment_lifecycle(self):
        jordan = self.as_user("jordan@akevra.test")
        alex = self.as_user("alex@akevra.test")

        issued = jordan.post(
            f"/api/v1/relationships/{self.rbt.id}/assignments",
            {"title": "Behavior Intervention Case Review", "instructions": "Draft a BIP.", "due_on": "2026-08-15"},
            format="json",
        )
        self.assertEqual(issued.status_code, 201, issued.data)
        assignment_id = issued.data["id"]
        self.assertEqual(issued.data["status"], "issued")

        self.assertEqual(alex.post(f"/api/v1/relationships/{self.rbt.id}/assignments", {"title": "x"}, format="json").status_code, 403)
        self.assertEqual(jordan.post(f"/api/v1/assignments/{assignment_id}/complete", {}, format="json").status_code, 400)

        rejected_type = alex.post(
            f"/api/v1/assignments/{assignment_id}/submit",
            {"files": [SimpleUploadedFile("tool.exe", b"MZ", content_type="application/octet-stream")]},
            format="multipart",
        )
        self.assertEqual(rejected_type.status_code, 400)
        self.assertIn("accepted file type", rejected_type.data["detail"])

        submitted = alex.post(
            f"/api/v1/assignments/{assignment_id}/submit",
            {"note": "First draft attached.", "files": [self.pdf("JW_BIP_Draft_v1.pdf")]},
            format="multipart",
        )
        self.assertEqual(submitted.status_code, 200, submitted.data)
        self.assertEqual(submitted.data["status"], "submitted")
        self.assertEqual(jordan.post(f"/api/v1/assignments/{assignment_id}/submit", {"note": "x"}, format="multipart").status_code, 403)

        no_comment = jordan.post(f"/api/v1/assignments/{assignment_id}/request-revision", {}, format="json")
        self.assertEqual(no_comment.status_code, 400)
        self.assertEqual(alex.post(f"/api/v1/assignments/{assignment_id}/request-revision", {"comment": "x"}, format="json").status_code, 403)
        revision = jordan.post(
            f"/api/v1/assignments/{assignment_id}/request-revision",
            {"comment": "Add proactive strategies."},
            format="json",
        )
        self.assertEqual(revision.status_code, 200, revision.data)
        self.assertEqual(revision.data["status"], "revision_requested")

        resubmitted = alex.post(
            f"/api/v1/assignments/{assignment_id}/submit",
            {"note": "Revised.", "files": [self.pdf("JW_BIP_Draft_v2.pdf", b"%PDF-1.4 v2")]},
            format="multipart",
        )
        self.assertEqual(resubmitted.status_code, 200, resubmitted.data)

        completed = jordan.post(f"/api/v1/assignments/{assignment_id}/complete", {"comment": "Approved."}, format="json")
        self.assertEqual(completed.status_code, 200, completed.data)
        self.assertEqual(completed.data["status"], "completed")
        self.assertEqual(alex.post(f"/api/v1/assignments/{assignment_id}/submit", {"note": "again"}, format="multipart").status_code, 400)

        history = alex.get(f"/api/v1/assignments/{assignment_id}").data
        self.assertEqual(
            [(e["from_status"], e["to_status"]) for e in history["events"]],
            [
                ("", "issued"),
                ("issued", "submitted"),
                ("submitted", "revision_requested"),
                ("revision_requested", "submitted"),
                ("submitted", "completed"),
            ],
        )
        self.assertEqual(history["events"][2]["comment"], "Add proactive strategies.")
        self.assertEqual([a["name"] for a in history["events"][1]["attachments"]], ["JW_BIP_Draft_v1.pdf"])
        self.assertEqual([a["name"] for a in history["events"][3]["attachments"]], ["JW_BIP_Draft_v2.pdf"])

        download = jordan.get(history["events"][1]["attachments"][0]["download_url"])
        self.assertEqual(download.status_code, 200)
        self.assertEqual(b"".join(download.streaming_content), b"%PDF-1.4 test")
        outsider = self.as_user("summit.supervisor@akevra.test")
        self.assertEqual(outsider.get(history["events"][1]["attachments"][0]["download_url"]).status_code, 404)

    # ------------------------------------------------------------------
    # 3. Two separate hours models; no calculation path mixes them
    # ------------------------------------------------------------------
    def test_03_hours_models_are_separate(self):
        jordan = self.as_user("jordan@akevra.test")
        alex = self.as_user("alex@akevra.test")
        casey = self.as_user("casey@akevra.test")

        # RBT ongoing: no self-logged hours, and the failed attempt leaves no cycle behind
        self_logged = alex.post(
            f"/api/v1/relationships/{self.rbt.id}/hours",
            {"occurred_on": "2026-09-03", "hours": "4", "kind": "supervision", "supervision_format": "individual"},
            format="json",
        )
        self.assertEqual(self_logged.status_code, 400)
        self.assertIn("session records", self_logged.data["detail"])
        self.assertFalse(MonthlyCycle.objects.filter(relationship=self.rbt, year=2026, month=9).exists())
        # Only the Supervisor authors the session records the RBT's supervision hours come from
        self.assertEqual(
            alex.post(f"/api/v1/relationships/{self.rbt.id}/sessions",
                      {"occurred_on": "2026-09-03", "duration_minutes": 60, "session_type": "individual"},
                      format="json").status_code,
            403,
        )
        jordan.post(f"/api/v1/relationships/{self.rbt.id}/sessions",
                    {"occurred_on": "2026-09-03", "duration_minutes": 120, "session_type": "individual",
                     "client_observation": True}, format="json")
        jordan.post(f"/api/v1/relationships/{self.rbt.id}/sessions",
                    {"occurred_on": "2026-09-17", "duration_minutes": 60, "session_type": "group"}, format="json")
        self.assertEqual(
            jordan.put(f"/api/v1/relationships/{self.rbt.id}/service-hours",
                       {"year": 2026, "month": 9, "hours": "60"}, format="json").status_code,
            403,
        )
        alex.put(f"/api/v1/relationships/{self.rbt.id}/service-hours", {"year": 2026, "month": 9, "hours": "60"}, format="json")
        rbt_month = alex.get(f"/api/v1/relationships/{self.rbt.id}/compliance").data["months"][-1]
        self.assertEqual(rbt_month["model"], "session_derived_supervision")
        self.assertEqual(rbt_month["supervision_hours"], 3.0)
        self.assertEqual(rbt_month["service_hours"], 60.0)
        self.assertEqual(rbt_month["supervision_percent"], 5.0)
        self.assertEqual(rbt_month["status"], "met")

        # Supervised Fieldwork: no service-hours attestation
        attest = casey.put(f"/api/v1/relationships/{self.fieldwork.id}/service-hours",
                           {"year": 2026, "month": 9, "hours": "100"}, format="json")
        self.assertEqual(attest.status_code, 400)

        # Self-logged hours count only once the Supervisor verifies them
        independent = self.log_hours(casey, "2026-09-02", "19")
        supervision = self.log_hours(casey, "2026-09-09", "1", kind="supervision",
                                     supervision_format="individual", client_observation=True)
        pending = casey.get(f"/api/v1/relationships/{self.fieldwork.id}/compliance").data["months"][-1]
        self.assertEqual(pending["model"], "fieldwork_verified_hours")
        self.assertEqual(pending["total_hours"], 0.0)

        self.assertEqual(casey.post(f"/api/v1/hours/{independent['id']}/verify").status_code, 403)
        self.assertEqual(jordan.post(f"/api/v1/hours/{independent['id']}/verify").status_code, 200)
        returned = jordan.post(f"/api/v1/hours/{supervision['id']}/return", {"reason": "Wrong date"}, format="json")
        self.assertEqual(returned.data["status"], "returned")
        corrected = casey.patch(f"/api/v1/hours/{supervision['id']}", {"occurred_on": "2026-09-10"}, format="json")
        self.assertEqual(corrected.data["status"], "pending")
        jordan.post(f"/api/v1/hours/{supervision['id']}/verify")
        self.assertEqual(casey.patch(f"/api/v1/hours/{supervision['id']}", {"hours": "9"}, format="json").status_code, 400)

        # A session documented on the fieldwork relationship never enters the fieldwork calculation
        jordan.post(f"/api/v1/relationships/{self.fieldwork.id}/sessions",
                    {"occurred_on": "2026-09-20", "duration_minutes": 600, "session_type": "individual"}, format="json")
        verified = casey.get(f"/api/v1/relationships/{self.fieldwork.id}/compliance").data["months"][-1]
        self.assertEqual(verified["total_hours"], 20.0)
        self.assertEqual(verified["supervision_hours"], 1.0)
        self.assertEqual(verified["supervision_percent"], 5.0)

        # And the RBT figures are untouched by the fieldwork activity
        again = alex.get(f"/api/v1/relationships/{self.rbt.id}/compliance").data["months"][-1]
        self.assertEqual((again["supervision_hours"], again["service_hours"]), (3.0, 60.0))

        # The separation also holds below the API
        rbt_cycle = MonthlyCycle.objects.get(relationship=self.rbt, year=2026, month=9)
        with self.assertRaises(ValidationError):
            HoursEntry.objects.create(organization=rbt_cycle.organization, cycle=rbt_cycle, hours=1,
                                      occurred_on=date(2026, 9, 1), attested_on=date(2026, 9, 1))

    # ------------------------------------------------------------------
    # 4. Calculations end to end, including mixed supervised/concentrated fieldwork
    # ------------------------------------------------------------------
    def test_04_mixed_fieldwork_cumulative_and_rule_override(self):
        jordan = self.as_user("jordan@akevra.test")
        casey = self.as_user("casey@akevra.test")
        alex = self.as_user("alex@akevra.test")

        # July: supervised month, 100 h with 5 h individual supervision across 4 contacts
        for day in range(1, 6):
            entry = self.log_hours(casey, f"2026-07-0{day}", "19")
            jordan.post(f"/api/v1/hours/{entry['id']}/verify")
        for index, day in enumerate(("2026-07-07", "2026-07-14", "2026-07-21", "2026-07-28")):
            entry = self.log_hours(casey, day, "1.25", kind="supervision", supervision_format="individual",
                                   client_observation=index == 0)
            jordan.post(f"/api/v1/hours/{entry['id']}/verify")

        # August: concentrated month, 100 h with 10 h supervision across 6 contacts
        for day in range(1, 6):
            entry = self.log_hours(casey, f"2026-08-0{day}", "18")
            jordan.post(f"/api/v1/hours/{entry['id']}/verify")
        aug_cycle = MonthlyCycle.objects.get(relationship=self.fieldwork, year=2026, month=8)
        self.assertEqual(casey.patch(f"/api/v1/cycles/{aug_cycle.id}", {"fieldwork_type": "concentrated"}, format="json").status_code, 403)
        switched = jordan.patch(f"/api/v1/cycles/{aug_cycle.id}", {"fieldwork_type": "concentrated"}, format="json")
        self.assertEqual(switched.status_code, 200, switched.data)
        for index, hours in enumerate(("1.5", "1.5", "1.5", "1.5", "1.5", "2.5")):
            entry = self.log_hours(casey, f"2026-08-{10 + index:02d}", hours, kind="supervision",
                                   supervision_format="individual", client_observation=index == 0)
            jordan.post(f"/api/v1/hours/{entry['id']}/verify")

        summary = casey.get(f"/api/v1/relationships/{self.fieldwork.id}/compliance").data
        july, aug = summary["months"]
        self.assertEqual((july["fieldwork_type"], july["status"], july["supervision_percent"]), ("supervised", "met", 5.0))
        self.assertEqual((aug["fieldwork_type"], aug["status"], aug["supervision_percent"]), ("concentrated", "met", 10.0))
        cumulative = summary["cumulative"]
        self.assertEqual(cumulative["supervised_hours"], 100.0)
        self.assertEqual(cumulative["concentrated_hours"], 100.0)
        # 100/2000 + 100/1500 = 5% + 6.67%
        self.assertEqual(cumulative["percent_complete"], 11.67)
        self.assertFalse(july["rules"]["approved"])

        # An approved rule set replaces the defaults from its effective date, with no code change
        rule_set = ComplianceRuleSet.objects.create(
            name="RBT Standard 2026 (test)", effective_from=date(2026, 9, 1),
            supervision_track=SupervisionTrack.RBT_ONGOING,
        )
        ComplianceRuleParameter.objects.create(rule_set=rule_set, key="min_supervision_percent", value="10")
        jordan.post(f"/api/v1/relationships/{self.rbt.id}/sessions",
                    {"occurred_on": "2026-09-03", "duration_minutes": 180, "session_type": "individual",
                     "client_observation": True}, format="json")
        jordan.post(f"/api/v1/relationships/{self.rbt.id}/sessions",
                    {"occurred_on": "2026-09-10", "duration_minutes": 60, "session_type": "individual"}, format="json")
        alex.put(f"/api/v1/relationships/{self.rbt.id}/service-hours", {"year": 2026, "month": 9, "hours": "80"}, format="json")
        month = alex.get(f"/api/v1/relationships/{self.rbt.id}/compliance").data["months"][-1]
        self.assertEqual(month["supervision_percent"], 5.0)
        self.assertEqual(month["required_supervision_percent"], 10.0)
        self.assertEqual(month["status"], "shortfall")
        self.assertEqual(month["remaining_supervision_hours"], 4.0)
        self.assertTrue(month["rules"]["approved"])
        self.assertEqual(month["rules"]["name"], "RBT Standard 2026 (test)")


# ---------------------------------------------------------------------------
# Calculation test-case set. BACB-based placeholders until AKEVRA supplies and approves
# its own set: replace or extend the rows below, the assertions stay the same.
# ---------------------------------------------------------------------------

RBT_CASES = [
    # id, service_hours, supervision_minutes, contacts, individual, observation, expected
    ("R1 exactly 5%", 100, 300, 2, 1, 1, {"status": "met", "supervision_percent": 5.0, "remaining_supervision_hours": 0.0}),
    ("R2 4% short by 1 h", 100, 240, 2, 1, 1, {"status": "shortfall", "supervision_percent": 4.0, "remaining_supervision_hours": 1.0}),
    ("R3 enough time, one contact", 160, 480, 1, 1, 1, {"status": "shortfall", "supervision_percent": 5.0}),
    ("R4 no individual contact", 100, 300, 2, 0, 1, {"status": "shortfall"}),
    ("R5 no observation", 100, 300, 2, 1, 0, {"status": "shortfall"}),
    ("R6 boundary 130 h / 390 min", 130, 390, 2, 1, 1, {"status": "met", "supervision_percent": 5.0, "required_supervision_hours": 6.5}),
    ("R7 not yet attested", None, 120, 2, 1, 1, {"status": "awaiting_service_hours", "met": False}),
    ("R8 no services this month", 0, 0, 0, 0, 0, {"status": "no_service_hours", "met": True}),
]

FIELDWORK_MONTH_CASES = [
    # id, type, total, supervision, group, contacts, observation, expected
    ("F1 supervised 5%", "supervised", 100, 5, 2, 4, 1, {"status": "met", "supervision_percent": 5.0, "countable_hours": 100.0}),
    ("F2 supervised 4.99%", "supervised", 100, "4.99", 0, 4, 1, {"status": "shortfall", "countable_hours": 0.0}),
    ("F3 concentrated 10%", "concentrated", 100, 10, 0, 6, 1, {"status": "met", "supervision_percent": 10.0}),
    ("F4 concentrated 8%", "concentrated", 100, 8, 0, 6, 1, {"status": "shortfall", "remaining_supervision_hours": 2.0}),
    ("F5 concentrated 5 contacts", "concentrated", 100, 10, 0, 5, 1, {"status": "shortfall"}),
    ("F6 under 20 h", "supervised", 15, 1, 0, 4, 1, {"status": "shortfall"}),
    ("F7 over 130 h", "supervised", 140, 7, 0, 4, 1, {"status": "shortfall"}),
    ("F8 group over 50%", "supervised", 100, 5, 3, 4, 1, {"status": "shortfall"}),
    ("F9 group exactly 50%", "supervised", 100, 5, "2.5", 4, 1, {"status": "met"}),
    ("F10 no observation", "supervised", 100, 5, 0, 4, 0, {"status": "shortfall"}),
]

CUMULATIVE_CASES = [
    # id, [(type, countable hours)], expected
    ("C1 supervised only", [("supervised", 2000)], {"percent_complete": 100.0, "complete": True}),
    ("C2 concentrated only", [("concentrated", 1500)], {"percent_complete": 100.0, "complete": True}),
    ("C3 mixed half and half", [("supervised", 1000), ("concentrated", 750)], {"percent_complete": 100.0, "complete": True}),
    ("C4 mixed partial", [("supervised", 1000), ("concentrated", 300)],
     {"percent_complete": 70.0, "remaining_if_supervised": 600.0, "remaining_if_concentrated": 450.0, "complete": False}),
    ("C5 over-accrued caps at 100", [("supervised", 2100)], {"percent_complete": 100.0}),
    ("C6 nothing yet", [], {"percent_complete": 0.0, "remaining_if_supervised": 2000.0}),
]


class CalculationCaseTests(SimpleTestCase):
    def assertSubset(self, expected, actual, case_id):
        for key, value in expected.items():
            self.assertEqual(actual[key], value, f"{case_id}: {key}")

    def test_rbt_ongoing_cases(self):
        rules = default_rules(SupervisionTrack.RBT_ONGOING)
        for case_id, service, minutes, contacts, individual, observation, expected in RBT_CASES:
            with self.subTest(case_id):
                result = evaluate_ongoing_month(
                    rules, service_hours=service, supervision_minutes=minutes, contacts=contacts,
                    individual_contacts=individual, observation_contacts=observation,
                )
                self.assertSubset(expected, result, case_id)

    def test_fieldwork_month_cases(self):
        rules = default_rules(SupervisionTrack.SUPERVISED_FIELDWORK)
        for case_id, kind, total, supervision, group, contacts, observation, expected in FIELDWORK_MONTH_CASES:
            with self.subTest(case_id):
                result = evaluate_fieldwork_month(
                    rules, kind, total_hours=total, supervision_hours=supervision, group_hours=group,
                    contacts=contacts, observation_contacts=observation,
                )
                self.assertSubset(expected, result, case_id)

    def test_fieldwork_cumulative_cases(self):
        rules = default_rules(SupervisionTrack.SUPERVISED_FIELDWORK)
        for case_id, months, expected in CUMULATIVE_CASES:
            with self.subTest(case_id):
                self.assertSubset(expected, evaluate_fieldwork_cumulative(rules, months), case_id)
