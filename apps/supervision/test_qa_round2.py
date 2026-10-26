from datetime import date, datetime, timedelta, timezone as dt_timezone

from django.core.management import call_command
from django.test import TestCase
from django.utils import timezone
from rest_framework.test import APIClient

from apps.accounts.models import UserAccount
from apps.core.tests import PASSWORD
from apps.supervision.models import Notification, SupervisionTrack, SupervisoryRelationship


class QaRound2Tests(TestCase):
    """The client's M4/M5 regression review: linked preparation stays reviewable and versioned,
    the conditional preparation field, notification time zone and wording, per-supervisor review
    of shared service hours, and the application-based fieldwork standard with the 2027
    observation-duration rule."""

    @classmethod
    def setUpTestData(cls):
        call_command("seed_sprint0", verbosity=0)
        cls.jordan = UserAccount.objects.get(email="jordan@akevra.test")
        cls.morgan = UserAccount.objects.get(email="morgan@akevra.test")
        cls.alex = UserAccount.objects.get(email="alex@akevra.test")
        cls.casey = UserAccount.objects.get(email="casey@akevra.test")
        cls.rbt = SupervisoryRelationship.objects.get(supervisor=cls.jordan, supervisee=cls.alex)

    def as_user(self, email):
        client = APIClient()
        response = client.post("/api/v1/auth/login", {"email": email, "password": PASSWORD}, format="json")
        client.credentials(HTTP_AUTHORIZATION=f"Bearer {response.data.get('token')}")
        return client

    def required_answers(self, client):
        form = client.get(f"/api/v1/relationships/{self.rbt.id}/preparation").data
        return form, {q["key"]: f"Answer for {q['key']}" for s in form["sections"] for q in s["questions"]
                      if q.get("required") and not q.get("show_if")}

    def schedule(self, client, start):
        return client.post(f"/api/v1/relationships/{self.rbt.id}/appointments",
                           {"starts_at": start.isoformat(), "ends_at": (start + timedelta(hours=1)).isoformat()},
                           format="json").data["appointments"][0]

    # ------------------------------------------------------------------
    # Preparation: reopen, review and edit the linked preparation; history is kept
    # ------------------------------------------------------------------
    def test_linked_preparation_is_reviewable_and_edits_are_versioned(self):
        jordan, alex = self.as_user("jordan@akevra.test"), self.as_user("alex@akevra.test")
        _, answers = self.required_answers(alex)
        alex.put(f"/api/v1/relationships/{self.rbt.id}/preparation", {"answers": answers}, format="json")
        appointment = self.schedule(jordan, timezone.now() + timedelta(days=5))

        # The supervisee can reopen it from the preparation endpoint (no blank form)
        form = alex.get(f"/api/v1/relationships/{self.rbt.id}/preparation").data
        self.assertIsNone(form["waiting"])
        self.assertEqual(len(form["linked"]), 1)
        linked = form["linked"][0]
        self.assertEqual(linked["appointment_id"], appointment["id"])
        self.assertEqual(linked["note"]["answers"], answers)
        self.assertEqual([r["version"] for r in linked["note"]["revisions"]], [1])

        # Editing keeps the original submission and adds a version
        edited = {**answers, "wins": "Edited after scheduling"}
        saved = alex.put(f"/api/v1/appointments/{appointment['id']}/prep-note", {"answers": edited}, format="json")
        self.assertEqual(saved.status_code, 200, saved.data)
        revisions = saved.data["revisions"]
        self.assertEqual([r["version"] for r in revisions], [1, 2])
        self.assertEqual(revisions[0]["answers"]["wins"], answers["wins"])
        self.assertEqual(revisions[1]["answers"]["wins"], "Edited after scheduling")
        self.assertFalse(revisions[0]["linked"])  # first submitted before the session existed
        # Saving the same answers again adds nothing
        again = alex.put(f"/api/v1/appointments/{appointment['id']}/prep-note", {"answers": edited}, format="json").data
        self.assertEqual(len(again["revisions"]), 2)
        # The supervisor sees the current answers and the history
        prep = jordan.get(f"/api/v1/appointments/{appointment['id']}").data["prep_note"]
        self.assertEqual((prep["answers"]["wins"], len(prep["revisions"])), ("Edited after scheduling", 2))

    def test_concern_details_only_apply_when_yes(self):
        alex = self.as_user("alex@akevra.test")
        form, answers = self.required_answers(alex)
        details = next(q for s in form["sections"] for q in s["questions"] if q["key"] == "ethics_details")
        self.assertEqual(details["show_if"]["key"], "ethics_concerns")
        url = f"/api/v1/relationships/{self.rbt.id}/preparation"
        # "No": a leftover description is dropped, not stored
        saved = alex.put(url, {"answers": {**answers, "ethics_concerns": "No", "ethics_details": "stale text"}}, format="json")
        self.assertEqual(saved.status_code, 200, saved.data)
        self.assertNotIn("ethics_details", saved.data["answers"])
        # "Yes": the description is required
        yes = details["show_if"]["equals"]
        self.assertEqual(alex.put(url, {"answers": {**answers, "ethics_concerns": yes}}, format="json").status_code, 400)
        ok = alex.put(url, {"answers": {**answers, "ethics_concerns": yes, "ethics_details": "Client safety"}}, format="json")
        self.assertEqual(ok.data["answers"]["ethics_details"], "Client safety")

    # ------------------------------------------------------------------
    # Notifications: organization time zone, preparation-aware wording
    # ------------------------------------------------------------------
    def test_notification_time_uses_organization_time_zone_and_knows_about_preparation(self):
        jordan, alex = self.as_user("jordan@akevra.test"), self.as_user("alex@akevra.test")
        self.assertEqual(self.jordan.organization.time_zone, "America/New_York")
        # 10:00 AM in New York on Oct 24 (EDT) is 14:00 UTC
        start = datetime(2026, 10, 24, 14, 0, tzinfo=dt_timezone.utc)
        _, answers = self.required_answers(alex)
        alex.put(f"/api/v1/relationships/{self.rbt.id}/preparation", {"answers": answers}, format="json")
        appointment = self.schedule(jordan, start)
        note = Notification.objects.filter(recipient=self.alex, appointment_id=appointment["id"], kind="appointment_scheduled").get()
        self.assertIn("Oct 24, 2026 at 10:00 AM EDT", note.title)
        self.assertNotIn("2:00 PM", note.title)
        self.assertIn("already linked", note.body)
        self.assertNotIn("Please complete your supervision preparation", note.body)
        self.assertTrue(Notification.objects.filter(recipient=self.jordan, appointment_id=appointment["id"], kind="preparation_submitted").exists())

        # Without preparation the request to prepare stays
        other = self.schedule(jordan, datetime(2026, 10, 31, 14, 0, tzinfo=dt_timezone.utc))
        body = Notification.objects.get(recipient=self.alex, appointment_id=other["id"], kind="appointment_scheduled").body
        self.assertIn("Please complete your supervision preparation", body)

    def test_organization_time_zone_setting(self):
        admin = APIClient()
        login = admin.post("/api/v1/auth/login", {"email": "settings.admin@akevra.test", "password": PASSWORD}, format="json")
        admin.credentials(HTTP_AUTHORIZATION=f"Bearer {login.data['token']}")
        self.assertEqual(admin.get("/api/v1/organization/settings").data["time_zone"], "America/New_York")
        self.assertEqual(admin.patch("/api/v1/organization/settings", {"time_zone": "Mars/Base"}, format="json").status_code, 400)
        self.assertEqual(admin.patch("/api/v1/organization/settings", {"time_zone": "America/Chicago"}, format="json").data["time_zone"], "America/Chicago")
        self.assertEqual(self.as_user("jordan@akevra.test").patch("/api/v1/organization/settings", {"time_zone": "UTC"}, format="json").status_code, 403)

    # ------------------------------------------------------------------
    # Shared service hours: each supervisor's review shown separately
    # ------------------------------------------------------------------
    def test_shared_service_hours_show_each_supervisors_review(self):
        second = SupervisoryRelationship.objects.create(
            organization=self.jordan.organization, supervisor=self.morgan, supervisee=self.alex,
            supervision_track=SupervisionTrack.RBT_ONGOING, started_on=date(2026, 7, 1),
        )
        jordan, morgan, alex = self.as_user("jordan@akevra.test"), self.as_user("morgan@akevra.test"), self.as_user("alex@akevra.test")
        record = alex.put(f"/api/v1/relationships/{self.rbt.id}/service-hours", {"year": 2026, "month": 8, "hours": "101"}, format="json").data
        reviewed = jordan.post(f"/api/v1/service-hours/{record['id']}/review", {"relationship_id": str(self.rbt.id)}, format="json").data
        status = {row["supervisor"]["name"]: row["status"] for row in reviewed["review_status"]}
        self.assertEqual(status, {self.jordan.full_name: "reviewed", self.morgan.full_name: "pending"})
        both = morgan.post(f"/api/v1/service-hours/{record['id']}/review", {"relationship_id": str(second.id)}, format="json").data
        self.assertEqual({row["status"] for row in both["review_status"]}, {"reviewed"})
        self.assertTrue(all(row["reviewed_at"] for row in both["review_status"]))

    # ------------------------------------------------------------------
    # Fieldwork: standard by application, determined per trainee; 2027 observation by duration
    # ------------------------------------------------------------------
    def fieldwork(self):
        return SupervisoryRelationship.objects.create(
            organization=self.jordan.organization, supervisor=self.jordan, supervisee=self.casey,
            supervision_track=SupervisionTrack.SUPERVISED_FIELDWORK, fieldwork_subtype="supervised", started_on=date(2026, 9, 1),
        )

    def log(self, client, rel, day, hours, **extra):
        response = client.post(f"/api/v1/relationships/{rel.id}/hours", {"occurred_on": day, "hours": hours, **extra}, format="json")
        self.assertEqual(response.status_code, 201, response.data)
        return response.data

    def test_fieldwork_standard_is_a_trainee_determination_tracking_both_until_known(self):
        jordan, casey = self.as_user("jordan@akevra.test"), self.as_user("casey@akevra.test")
        fw = self.fieldwork()
        self.assertEqual(jordan.get(f"/api/v1/relationships/{fw.id}").data["fieldwork_requirements"], "both")

        # October 2026: 4 individual contacts of 1 h; one observed contact of only 30 minutes
        for day in ("2026-10-01", "2026-10-02", "2026-10-03", "2026-10-06"):
            self.log(casey, fw, day, "19", kind="independent")
        for day in ("2026-10-05", "2026-10-12", "2026-10-19", "2026-10-26"):
            extra = {"client_observation": True, "observation_minutes": 30} if day == "2026-10-05" else {}
            self.log(casey, fw, day, "1", kind="supervision", supervision_format="individual", **extra)
        # A new observation must give its duration
        missing = casey.post(f"/api/v1/relationships/{fw.id}/hours", {"occurred_on": "2026-10-27", "hours": "1", "kind": "supervision",
                                                                       "supervision_format": "individual", "client_observation": True}, format="json")
        self.assertEqual(missing.status_code, 400)
        for entry in casey.get(f"/api/v1/relationships/{fw.id}/hours").data["entries"]:
            jordan.post(f"/api/v1/hours/{entry['id']}/verify")

        def october():
            data = jordan.get(f"/api/v1/relationships/{fw.id}/compliance").data
            return next(m for m in data["months"] if m["month"] == 10), data["cumulative"]

        # Undetermined: checked against both; the month counts only what both standards accept
        month, cumulative = october()
        self.assertEqual(month["standard"], "both")
        self.assertTrue(month["by_standard"]["bacb_2022"]["met"])
        self.assertFalse(month["by_standard"]["bacb_2027"]["met"])
        self.assertEqual((month["status"], month["eligible_hours"]), ("shortfall", 0.0))
        observed = next(c for c in month["checks"] if c["key"] == "observation_minutes:2027")
        self.assertEqual((observed["required"], observed["actual"]), (60, 30))
        self.assertEqual(cumulative["standards"], ["bacb_2022", "bacb_2027"])

        # A determination needs a basis, and must agree with the application year
        url = f"/api/v1/relationships/{fw.id}"
        self.assertEqual(jordan.patch(url, {"fieldwork_requirements": "bacb_2022"}, format="json").status_code, 400)
        wrong_year = jordan.patch(url, {"fieldwork_requirements": "bacb_2022", "fieldwork_standard_basis": "application_year",
                                        "fieldwork_application_year": 2027}, format="json")
        self.assertEqual(wrong_year.status_code, 400)
        self.assertIn("before January 1, 2027", str(wrong_year.data))
        self.assertEqual(jordan.patch(url, {"fieldwork_requirements": "bacb_2027", "fieldwork_standard_basis": "supervisor_decision"},
                                      format="json").status_code, 400)  # a decision needs a note
        decided = jordan.patch(url, {"fieldwork_requirements": "bacb_2022", "fieldwork_standard_basis": "application_year",
                                     "fieldwork_application_year": 2026, "fieldwork_standard_note": "Application planned for Nov 2026"},
                               format="json").data
        self.assertEqual((decided["fieldwork_standard_determined_by"], decided["fieldwork_standard_determined_at"] is not None),
                         (self.jordan.full_name, True))
        self.assertEqual(casey.patch(url, {"fieldwork_requirements": "bacb_2027"}, format="json").status_code, 403)

        # 2022 standard: one observed contact meets the month
        month, _ = october()
        self.assertEqual((month["standard"], month["status"], month["eligible_hours"]), ("bacb_2022", "met", 80.0))

        # 2027 standard applies to the 2026 month too (by application, not by calendar month)
        jordan.patch(url, {"fieldwork_requirements": "bacb_2027", "fieldwork_application_year": 2027}, format="json")
        month, _ = october()
        self.assertEqual((month["standard"], month["status"], month["eligible_hours"]), ("bacb_2027", "shortfall", 0.0))
        self.assertIn("below 60 minutes", month["adjustments"][0]["detail"])
        self.assertNotIn("observation_contacts", {c["key"] for c in month["checks"]})

        # The supervisor can record the real duration on the verified observed entry; only that changes
        observed_entry = next(e for e in casey.get(f"/api/v1/relationships/{fw.id}/hours").data["entries"] if e["client_observation"])
        url = f"/api/v1/hours/{observed_entry['id']}/observation-minutes"
        self.assertEqual(casey.post(url, {"observation_minutes": 60}, format="json").status_code, 403)
        self.assertEqual(jordan.post(url, {"observation_minutes": 0}, format="json").status_code, 400)
        updated = jordan.post(url, {"observation_minutes": 60}, format="json").data
        self.assertEqual((updated["observation_minutes"], updated["status"], updated["hours"]), (60, "verified", 1.0))
        month, _ = october()
        self.assertEqual((month["status"], month["eligible_hours"]), ("met", 80.0))
        # Each correction is kept: who changed it, from what, to what; the supervisee can read it
        jordan.post(url, {"observation_minutes": 45}, format="json")
        history = casey.get(url).data
        self.assertEqual(history["observation_minutes"], 45)
        self.assertEqual([(h["before"], h["after"], h["by"]) for h in history["history"]],
                         [(observed_entry["observation_minutes"], 60, self.jordan.full_name), (60, 45, self.jordan.full_name)])
