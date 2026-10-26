from datetime import date, timedelta

from django.core.management import call_command
from django.test import TestCase
from django.utils import timezone
from rest_framework.test import APIClient

from apps.accounts.models import UserAccount
from apps.core.tests import PASSWORD
from apps.supervision.models import SupervisionTrack, SupervisoryRelationship


class QaRoundTests(TestCase):
    """The client's fresh-QA findings: relationship-scoped calculations, duplicate protection,
    documenting the same event, rule-set approval, and the M4/M5 completions."""

    @classmethod
    def setUpTestData(cls):
        call_command("seed_sprint0", verbosity=0)
        cls.jordan = UserAccount.objects.get(email="jordan@akevra.test")
        cls.morgan = UserAccount.objects.get(email="morgan@akevra.test")
        cls.alex = UserAccount.objects.get(email="alex@akevra.test")
        cls.casey = UserAccount.objects.get(email="casey@akevra.test")
        cls.admin = UserAccount.objects.filter(email="admin@akevra.test", organization=cls.jordan.organization).first()
        cls.rbt = SupervisoryRelationship.objects.get(supervisor=cls.jordan, supervisee=cls.alex)
        # The QA situation: a second relationship for the same RBT that has no sessions of its own
        cls.second = SupervisoryRelationship.objects.create(
            organization=cls.jordan.organization, supervisor=cls.morgan, supervisee=cls.alex,
            supervision_track=SupervisionTrack.RBT_ONGOING, started_on=date(2026, 7, 1),
        )

    def as_user(self, email):
        client = APIClient()
        response = client.post("/api/v1/auth/login", {"email": email, "password": PASSWORD}, format="json")
        if response.data.get("status") == "workspace_required":
            response = client.post("/api/v1/auth/select-workspace", {"organization_id": str(self.jordan.organization_id)}, format="json") \
                if False else response
        client.credentials(HTTP_AUTHORIZATION=f"Bearer {response.data.get('token')}")
        return client

    def session(self, client, rel, day, **extra):
        body = {"occurred_on": day, "start_time": "10:00", "end_time": "11:00", "session_type": "individual", "meeting_method": "in_person", **extra}
        return client.post(f"/api/v1/relationships/{rel.id}/sessions", body, format="json")

    def month(self, client, rel, month):
        return next(m for m in client.get(f"/api/v1/relationships/{rel.id}/compliance").data["months"] if m["month"] == month)

    def test_relationship_scoped_calculations(self):
        jordan, alex = self.as_user("jordan@akevra.test"), self.as_user("alex@akevra.test")
        alex.put(f"/api/v1/relationships/{self.rbt.id}/service-hours", {"year": 2026, "month": 8, "hours": "100"}, format="json")
        self.assertEqual(self.session(jordan, self.rbt, "2026-08-04", client_observation=True).status_code, 201)
        morgan = self.as_user("morgan@akevra.test")
        empty = self.month(morgan, self.second, 8)
        # Shared denominator, but no borrowed supervision, contacts or observations
        self.assertEqual((empty["service_hours"], empty["supervision_hours"], empty["contacts"]), (100.0, 0.0, 0))
        checks = {c["key"]: c["actual"] for c in empty["checks"]}
        self.assertEqual((checks["individual_contacts"], checks["observation_contacts"]), (0, 0))
        self.assertEqual(empty["overall"]["supervision_hours"], 1.0)
        # Adding to the first relationship never moves the second
        self.assertEqual(self.session(jordan, self.rbt, "2026-08-12").status_code, 201)
        self.assertEqual(self.month(morgan, self.second, 8)["supervision_hours"], 0.0)
        self.assertEqual(self.month(jordan, self.rbt, 8)["supervision_hours"], 2.0)

    def test_duplicate_sessions_and_documenting_the_same_event(self):
        jordan = self.as_user("jordan@akevra.test")
        self.assertEqual(self.session(jordan, self.rbt, "2026-08-04").status_code, 201)
        duplicate = self.session(jordan, self.rbt, "2026-08-04")
        self.assertEqual(duplicate.status_code, 400)
        self.assertIn("already documented", str(duplicate.data))
        overlapping = self.session(jordan, self.rbt, "2026-08-04", start_time="10:30", end_time="11:30")
        self.assertEqual(overlapping.status_code, 400)
        # Another supervisor can't add a second credit for the same time either
        self.assertEqual(self.session(self.as_user("morgan@akevra.test"), self.second, "2026-08-04").status_code, 400)
        self.assertEqual(self.session(jordan, self.rbt, "2026-08-04", start_time="13:00", end_time="14:00").status_code, 201)
        # Completing a draft that duplicates is refused too
        draft = self.session(jordan, self.rbt, "2026-08-04", start_time="13:30", end_time="14:30", draft=True).data
        refused = jordan.patch(f"/api/v1/sessions/{draft['id']}", {"complete": True}, format="json")
        self.assertEqual(refused.status_code, 400)

        # Scheduled → Occurred → Documentation Pending → document the same event → Completed
        start = timezone.now() - timedelta(days=2)
        appt = jordan.post(f"/api/v1/relationships/{self.rbt.id}/appointments",
                           {"starts_at": start.isoformat(), "ends_at": (start + timedelta(hours=1)).isoformat(), "meeting_method": "in_person"},
                           format="json").data["appointments"][0]
        held = jordan.post(f"/api/v1/appointments/{appt['id']}/complete").data
        self.assertEqual((held["status"], held["session"]), ("completed", None))
        day = timezone.localtime(start).date().isoformat()
        hhmm = timezone.localtime(start).strftime("%H:%M")
        end = timezone.localtime(start + timedelta(hours=1)).strftime("%H:%M")
        documented = jordan.post(f"/api/v1/relationships/{self.rbt.id}/sessions",
                                 {"occurred_on": day, "start_time": hhmm, "end_time": end, "session_type": "individual",
                                  "meeting_method": "in_person", "appointment_id": appt["id"]}, format="json")
        self.assertEqual(documented.status_code, 201, documented.data)
        self.assertEqual(jordan.get(f"/api/v1/appointments/{appt['id']}").data["session"]["id"], documented.data["id"])
        again = jordan.post(f"/api/v1/relationships/{self.rbt.id}/sessions",
                            {"occurred_on": day, "start_time": "23:00", "end_time": "23:30", "session_type": "individual",
                             "appointment_id": appt["id"]}, format="json")
        self.assertEqual(again.status_code, 400)
        self.assertIn("already documented", str(again.data))

    def test_rule_set_approval_by_administrator(self):
        jordan = self.as_user("jordan@akevra.test")
        catalog = jordan.get("/api/v1/compliance-rules").data
        self.assertFalse(catalog["can_approve"])
        rbt = next(t for t in catalog["tracks"] if t["track"] == "rbt_ongoing_supervision")
        self.assertFalse(rbt["approved"])
        self.assertEqual(jordan.post("/api/v1/compliance-rules/approve", {"track": "rbt_ongoing_supervision", "effective_from": "2026-01-01"}, format="json").status_code, 403)

        admin = APIClient()
        login = admin.post("/api/v1/auth/login", {"email": "settings.admin@akevra.test", "password": PASSWORD}, format="json")
        admin.credentials(HTTP_AUTHORIZATION=f"Bearer {login.data['token']}")
        refused = admin.post("/api/v1/compliance-rules/approve", {"track": "rbt_ongoing_supervision", "effective_from": "2026-01-01",
                                                                  "overrides": {"current": {"min_supervision_percent": "-1"}}}, format="json")
        self.assertEqual(refused.status_code, 400)
        approved = admin.post("/api/v1/compliance-rules/approve", {"track": "rbt_ongoing_supervision", "effective_from": "2026-01-01",
                                                                   "note": "Reviewed against the RBT Handbook"}, format="json")
        self.assertEqual(approved.status_code, 201, approved.data)
        rbt = next(t for t in approved.data["tracks"] if t["track"] == "rbt_ongoing_supervision")
        self.assertTrue(rbt["approved"])
        self.assertEqual(rbt["in_force"]["approval_note"], "Reviewed against the RBT Handbook")
        # Results now show the approved, organization-scoped rule set
        alex = self.as_user("alex@akevra.test")
        alex.put(f"/api/v1/relationships/{self.rbt.id}/service-hours", {"year": 2026, "month": 8, "hours": "40"}, format="json")
        rules = self.month(jordan, self.rbt, 8)["rules"]
        self.assertTrue(rules["approved"])
        self.assertEqual((rules["scope"], rules["approved_by"]), ("organization", login.data["user"].get("full_name") or rules["approved_by"]))
        # A new approval supersedes the earlier one, which stays on file
        admin.post("/api/v1/compliance-rules/approve", {"track": "rbt_ongoing_supervision", "effective_from": "2026-01-01"}, format="json")
        approvals = next(t for t in admin.get("/api/v1/compliance-rules").data["tracks"] if t["track"] == "rbt_ongoing_supervision")["approvals"]
        self.assertEqual(sorted(a["status"] for a in approvals), ["active", "superseded"])

    def test_external_reporting_requirement_and_reviews(self):
        jordan, morgan, alex = self.as_user("jordan@akevra.test"), self.as_user("morgan@akevra.test"), self.as_user("alex@akevra.test")
        admin = APIClient()
        login = admin.post("/api/v1/auth/login", {"email": "settings.admin@akevra.test", "password": PASSWORD}, format="json")
        admin.credentials(HTTP_AUTHORIZATION=f"Bearer {login.data['token']}")
        self.assertEqual(jordan.patch("/api/v1/organization/settings", {"require_external_reporting": True}, format="json").status_code, 403)
        self.assertTrue(admin.patch("/api/v1/organization/settings", {"require_external_reporting": True}, format="json").data["require_external_reporting"])

        record = alex.put(f"/api/v1/relationships/{self.rbt.id}/service-hours", {"year": 2026, "month": 7, "hours": "60"}, format="json").data
        self.session(jordan, self.rbt, "2026-07-08")
        cycle = next(c for c in jordan.get(f"/api/v1/relationships/{self.rbt.id}/cycles").data["cycles"] if c["month"] == 7)
        blocked = jordan.post(f"/api/v1/cycles/{cycle['id']}/status", {"status": "closed"}, format="json")
        self.assertEqual(blocked.status_code, 400)
        self.assertIn("other-agency reporting", str(blocked.data))
        none = alex.put(f"/api/v1/relationships/{self.rbt.id}/service-hours", {"year": 2026, "month": 7, "hours": "0", "scope": "external_none"}, format="json")
        self.assertEqual(none.status_code, 200, none.data)
        self.assertEqual(jordan.post(f"/api/v1/cycles/{cycle['id']}/status", {"status": "closed"}, format="json").data["status"], "closed")

        # Each supervisor reviews the shared record for their own relationship
        self.assertEqual(jordan.post(f"/api/v1/service-hours/{record['id']}/review", {"relationship_id": str(self.rbt.id)}, format="json").status_code, 200)
        self.assertEqual(jordan.post(f"/api/v1/service-hours/{record['id']}/review", {"relationship_id": str(self.rbt.id)}, format="json").status_code, 400)
        reviewed = morgan.post(f"/api/v1/service-hours/{record['id']}/review", {"relationship_id": str(self.second.id)}, format="json").data
        self.assertEqual(sorted(r["reviewer"]["name"] for r in reviewed["reviews"]), sorted([self.jordan.full_name, self.morgan.full_name]))
        self.assertEqual({r["relationship_id"] for r in reviewed["reviews"]}, {str(self.rbt.id), str(self.second.id)})

    def test_ongoing_service_hours_periods(self):
        alex, jordan = self.as_user("alex@akevra.test"), self.as_user("jordan@akevra.test")
        url = f"/api/v1/relationships/{self.rbt.id}/service-hours/periods"
        first = alex.post(url, {"start_date": "2026-09-01", "end_date": "2026-09-10", "hours": "30"}, format="json")
        self.assertEqual(first.status_code, 201, first.data)
        self.assertEqual((first.data["record"]["hours"], first.data["last_confirmed_through"]), (30.0, "2026-09-10"))
        second = alex.post(url, {"start_date": "2026-09-11", "end_date": "2026-09-20", "hours": "0", "zero_reason": "illness"}, format="json")
        self.assertEqual(second.status_code, 201, second.data)
        third = alex.post(url, {"start_date": "2026-09-21", "end_date": "2026-09-30", "hours": "25"}, format="json").data
        self.assertEqual(third["record"]["hours"], 55.0)
        self.assertEqual(len(third["record"]["periods"]), 3)
        self.assertEqual(alex.post(url, {"start_date": "2026-09-15", "end_date": "2026-09-16", "hours": "2"}, format="json").status_code, 400)
        self.assertEqual(alex.post(url, {"start_date": "2026-09-28", "end_date": "2026-10-02", "hours": "2"}, format="json").status_code, 400)
        self.assertEqual(jordan.post(url, {"start_date": "2026-10-01", "end_date": "2026-10-02", "hours": "2"}, format="json").status_code, 403)
        history = jordan.get(f"/api/v1/supervisees/{self.alex.id}/service-hours").data
        self.assertEqual(history["last_confirmed_through"], "2026-09-30")
        self.assertEqual(self.month(jordan, self.rbt, 9)["service_hours"], 55.0)

    def test_prior_hours_fieldwork_basis_and_open_months(self):
        jordan = self.as_user("jordan@akevra.test")
        bcaba = SupervisoryRelationship.objects.create(
            organization=self.jordan.organization, supervisor=self.jordan, supervisee=self.casey,
            supervision_track=SupervisionTrack.BCABA_ONGOING, started_on=date(2026, 9, 1),
        )
        updated = jordan.patch(f"/api/v1/relationships/{bcaba.id}", {"prior_service_hours": "950", "prior_service_hours_source": "Previous employer letter",
                                                                     "prior_service_hours_as_of": "2026-08-31"}, format="json").data
        detail = updated["prior_service_hours_detail"]
        self.assertEqual((detail["hours"], detail["source"], detail["as_of"], detail["entered_by"], detail["reviewed_by"]),
                         (950.0, "Previous employer letter", "2026-08-31", self.jordan.full_name, None))
        self.assertEqual(jordan.post(f"/api/v1/relationships/{bcaba.id}/prior-hours/review").data["prior_service_hours_detail"]["reviewed_by"], self.jordan.full_name)

        fw = SupervisoryRelationship.objects.create(
            organization=self.jordan.organization, supervisor=self.jordan, supervisee=self.casey,
            supervision_track=SupervisionTrack.SUPERVISED_FIELDWORK, fieldwork_subtype="supervised", started_on=date(2026, 9, 1),
        )
        jordan.patch(f"/api/v1/relationships/{fw.id}", {"fieldwork_requirements": "bacb_2027", "fieldwork_standard_basis": "application_year",
                                                       "fieldwork_application_year": 2027, "fieldwork_standard_note": "Applying after Jan 1, 2027"}, format="json")
        casey = self.as_user("casey@akevra.test")
        casey.post(f"/api/v1/relationships/{fw.id}/hours", {"occurred_on": "2026-10-02", "hours": "20", "kind": "independent"}, format="json")
        october = self.month(jordan, fw, 10)
        # The trainee's determined standard applies to an October 2026 month, not the calendar month's
        self.assertIn("2027", october["rules"]["name"])
        self.assertNotIn("2022", october["rules"]["name"])

        # An open month is not counted as "not met"
        alex = self.as_user("alex@akevra.test")
        today = date.today()
        alex.put(f"/api/v1/relationships/{self.rbt.id}/service-hours", {"year": today.year, "month": today.month, "hours": "100"}, format="json")
        cumulative = jordan.get(f"/api/v1/relationships/{self.rbt.id}/compliance").data["cumulative"]
        self.assertEqual((cumulative["months_open"], cumulative["months_evaluated"]), (1, cumulative["months_total"] - 1))

    def test_structured_and_standalone_preparation(self):
        jordan, alex = self.as_user("jordan@akevra.test"), self.as_user("alex@akevra.test")
        form = alex.get(f"/api/v1/relationships/{self.rbt.id}/preparation").data
        self.assertEqual((form["key"], form["can_prepare"]), ("rbt", True))
        required = [q["key"] for s in form["sections"] for q in s["questions"] if q.get("required")]
        self.assertTrue(required)
        incomplete = alex.put(f"/api/v1/relationships/{self.rbt.id}/preparation", {"answers": {"wins": "Good week"}}, format="json")
        self.assertEqual(incomplete.status_code, 400)
        answers = {key: f"Answer for {key}" for key in required}
        waiting = alex.put(f"/api/v1/relationships/{self.rbt.id}/preparation", {"answers": answers}, format="json")
        self.assertEqual(waiting.status_code, 200, waiting.data)
        self.assertIsNone(waiting.data["appointment_id"])
        self.assertEqual(jordan.put(f"/api/v1/relationships/{self.rbt.id}/preparation", {"answers": answers}, format="json").status_code, 403)
        # Scheduling attaches the waiting preparation; the supervisor sees it as Submitted
        start = timezone.now() + timedelta(days=5)
        appt = jordan.post(f"/api/v1/relationships/{self.rbt.id}/appointments",
                           {"starts_at": start.isoformat(), "ends_at": (start + timedelta(hours=1)).isoformat()}, format="json").data["appointments"][0]
        prep = jordan.get(f"/api/v1/appointments/{appt['id']}").data["prep_note"]
        self.assertEqual((prep["template_key"], prep["answers"][required[0]]), ("rbt", f"Answer for {required[0]}"))
        self.assertIn(form["sections"][0]["questions"][0]["label"], prep["body"])

    def test_audit_from_to_range(self):
        jordan = self.as_user("jordan@akevra.test")
        log = jordan.get(f"/api/v1/supervisees/{self.alex.id}/audit-log?start=2026-03&end=2026-08").data
        self.assertEqual([(r["year"], r["month"]) for r in log["rows"]][::5], [(2026, 3), (2026, 8)])
        self.assertEqual(len(log["rows"]), 6)
        self.assertEqual(log["summary"]["months"], 6)  # the summary covers the selected months only
        self.assertEqual(jordan.get(f"/api/v1/supervisees/{self.alex.id}/audit-log?start=2026-08&end=2026-03").status_code, 400)
