from django.test import TestCase, TransactionTestCase
from rest_framework.test import APIClient

from apps.accounts.models import UserAccount
from apps.core.tests import PASSWORD
from apps.rbac.matrix import MATRIX
from apps.rbac.models import OrgRole, RelationshipRole
from apps.supervision.models import (
    DevelopmentPlan,
    SuperviseeAssignmentPurpose,
    SupervisionTrack,
    SupervisoryRelationship,
)


class SupervisionSetupAcceptanceTests(TestCase):
    """Maps 1:1 to the Supervision Setup & Development Plan milestone."""

    @classmethod
    def setUpTestData(cls):
        from django.core.management import call_command

        call_command("seed_sprint0", verbosity=0)

    def setUp(self):
        self.client = APIClient()

    def auth(self, email, organization_name=None, password=PASSWORD):
        response = self.client.post(
            "/api/v1/auth/login",
            {"email": email, "password": password},
            format="json",
        )
        token = response.data.get("token")
        self.assertIsNotNone(token, response.data)
        self.client.credentials(HTTP_AUTHORIZATION=f"Bearer {token}")
        if response.data.get("status") == "workspace_required":
            orgs = response.data["organizations"]
            if organization_name:
                chosen = next(o for o in orgs if o["name"] == organization_name)
            else:
                chosen = orgs[0]
            response = self.client.post(
                "/api/v1/auth/workspace/select",
                {"organization_id": chosen["id"]},
                format="json",
            )
        self.assertEqual(response.data.get("status"), "authenticated", response.data)
        return response

    def _plan_payload(self, summary="Initial IDP"):
        return {
            "summary": summary,
            "goals": [
                {
                    "category": "data_collection",
                    "title": "Independent data collection",
                    "description": "Collect IOA without prompts",
                }
            ],
            "competencies": [
                {
                    "name": "Session documentation",
                    "description": "Write complete session notes",
                    "target_level": "independent",
                    "current_level": "developing",
                }
            ],
            "milestones": [
                {
                    "title": "Complete first documented supervision month",
                    "status": "active",
                }
            ],
        }

    def test_01_relationship_enforces_version_23_role_model(self):
        jordan = UserAccount.objects.get(email="jordan@akevra.test")
        casey = UserAccount.objects.get(email="casey@akevra.test")
        alex = UserAccount.objects.get(email="alex@akevra.test")
        self.assertEqual(casey.credential_type, "bcaba")

        self.auth("jordan@akevra.test")
        parties = self.client.get("/api/v1/eligible-parties")
        self.assertEqual(parties.status_code, 200)
        supervisor_emails = {row["email"] for row in parties.data["supervisors"]}
        supervisee_emails = {row["email"] for row in parties.data["supervisees"]}
        self.assertIn("jordan@akevra.test", supervisor_emails)
        self.assertNotIn("casey@akevra.test", supervisor_emails)
        self.assertNotIn("alex@akevra.test", supervisor_emails)
        self.assertIn("casey@akevra.test", supervisee_emails)
        self.assertIn("jordan@akevra.test", supervisee_emails)

        blocked_bcaba = self.client.post(
            "/api/v1/relationships",
            {
                "supervisor_id": str(casey.id),
                "supervisee_id": str(alex.id),
                "supervision_track": SupervisionTrack.BCABA_ONGOING,
                "started_on": "2026-09-01",
            },
            format="json",
        )
        self.assertEqual(blocked_bcaba.status_code, 400)
        self.assertEqual(blocked_bcaba.data["code"], "supervisor_must_be_bcba")

        blocked_rbt = self.client.post(
            "/api/v1/relationships",
            {
                "supervisor_id": str(alex.id),
                "supervisee_id": str(casey.id),
                "supervision_track": SupervisionTrack.RBT_ONGOING,
                "started_on": "2026-09-01",
            },
            format="json",
        )
        self.assertEqual(blocked_rbt.status_code, 400)
        self.assertEqual(blocked_rbt.data["code"], "supervisor_must_be_bcba")

        created = self.client.post(
            "/api/v1/relationships",
            {
                "supervisor_id": str(jordan.id),
                "supervisee_id": str(casey.id),
                "supervision_track": SupervisionTrack.BCABA_ONGOING,
                "started_on": "2026-09-01",
            },
            format="json",
        )
        self.assertEqual(created.status_code, 201, created.data)
        self.assertTrue(created.data["is_compliance_bearing"])
        self.assertEqual(created.data["supervisee"]["credential_type"], "bcaba")

        self.client = APIClient()
        self.auth("casey@akevra.test")
        self_as_supervisor = self.client.post(
            "/api/v1/relationships",
            {
                "supervisor_id": str(casey.id),
                "supervisee_id": str(alex.id),
                "supervision_track": SupervisionTrack.BCABA_ONGOING,
                "started_on": "2026-09-01",
            },
            format="json",
        )
        self.assertEqual(self_as_supervisor.status_code, 400)

    def test_02_exactly_one_intake_per_relationship(self):
        rel = SupervisoryRelationship.objects.get(supervisee__email="alex@akevra.test")
        self.auth("jordan@akevra.test")
        first = self.client.post(
            f"/api/v1/relationships/{rel.id}/intake",
            {
                "captured_on": "2026-09-01",
                "background": "RBT joining ongoing supervision",
                "notes": "Initial intake",
            },
            format="json",
        )
        self.assertEqual(first.status_code, 201, first.data)
        self.assertEqual(first.data["relationship_id"], str(rel.id))

        duplicate = self.client.post(
            f"/api/v1/relationships/{rel.id}/intake",
            {"captured_on": "2026-09-02", "notes": "Second attempt"},
            format="json",
        )
        self.assertEqual(duplicate.status_code, 409)
        self.assertEqual(duplicate.data["code"], "intake_duplicate")

        fetched = self.client.get(f"/api/v1/relationships/{rel.id}/intake")
        self.assertEqual(fetched.status_code, 200)
        self.assertEqual(fetched.data["id"], first.data["id"])
        self.assertEqual(fetched.data["notes"], "Initial intake")

    def test_03_idp_versioning_preserves_prior_version(self):
        rel = SupervisoryRelationship.objects.get(supervisee__email="alex@akevra.test")
        self.auth("jordan@akevra.test")
        created = self.client.post(
            f"/api/v1/relationships/{rel.id}/development-plans",
            self._plan_payload("Version 1 summary"),
            format="json",
        )
        self.assertEqual(created.status_code, 201, created.data)
        v1_id = created.data["id"]
        self.assertEqual(created.data["version_number"], 1)
        self.assertTrue(created.data["is_current"])
        self.assertEqual(len(created.data["goals"]), 1)
        self.assertEqual(len(created.data["competencies"]), 1)
        self.assertEqual(len(created.data["milestones"]), 1)

        revised = self.client.patch(
            f"/api/v1/development-plans/{v1_id}",
            {
                "summary": "Version 2 summary",
                "goals": [
                    {"title": "Independent data collection"},
                    {"title": "Lead a caregiver meeting"},
                ],
                "competencies": created.data["competencies"],
                "milestones": created.data["milestones"],
            },
            format="json",
        )
        self.assertEqual(revised.status_code, 200, revised.data)
        v2_id = revised.data["id"]
        self.assertNotEqual(v2_id, v1_id)
        self.assertEqual(revised.data["version_number"], 2)
        self.assertTrue(revised.data["is_current"])
        self.assertEqual(revised.data["summary"], "Version 2 summary")
        self.assertEqual(len(revised.data["goals"]), 2)
        self.assertEqual(revised.data["plan_family_id"], created.data["plan_family_id"])

        prior = self.client.get(f"/api/v1/development-plans/{v1_id}")
        self.assertEqual(prior.status_code, 200)
        self.assertEqual(prior.data["summary"], "Version 1 summary")
        self.assertEqual(prior.data["version_number"], 1)
        self.assertFalse(prior.data["is_current"])
        self.assertEqual(prior.data["record_status"], "superseded")
        self.assertEqual(len(prior.data["goals"]), 1)
        self.assertEqual(prior.data["goals"][0]["title"], "Independent data collection")
        self.assertEqual(len(prior.data["competencies"]), 1)
        self.assertEqual(len(prior.data["milestones"]), 1)

        v1 = DevelopmentPlan.objects.get(pk=v1_id)
        v2 = DevelopmentPlan.objects.get(pk=v2_id)
        self.assertNotEqual(v1.summary, v2.summary)
        self.assertEqual(v1.plan_family_id, v2.plan_family_id)

        listing = self.client.get(f"/api/v1/relationships/{rel.id}/development-plans")
        self.assertEqual(listing.status_code, 200)
        self.assertEqual(len(listing.data["plans"]), 2)

    def test_04_milestone_transition_requires_rationale(self):
        rel = SupervisoryRelationship.objects.get(supervisee__email="alex@akevra.test")
        self.auth("jordan@akevra.test")
        created = self.client.post(
            f"/api/v1/relationships/{rel.id}/development-plans",
            self._plan_payload(),
            format="json",
        )
        milestone_id = created.data["milestones"][0]["id"]

        rejected = self.client.post(
            f"/api/v1/milestones/{milestone_id}/transition",
            {"status": "achieved"},
            format="json",
        )
        self.assertEqual(rejected.status_code, 400)
        self.assertIn("rationale", rejected.data["detail"].lower())

        still_active = self.client.get(f"/api/v1/development-plans/{created.data['id']}")
        self.assertEqual(still_active.data["milestones"][0]["status"], "active")

        achieved = self.client.post(
            f"/api/v1/milestones/{milestone_id}/transition",
            {
                "status": "achieved",
                "rationale": "IOA met criterion across three consecutive sessions.",
            },
            format="json",
        )
        self.assertEqual(achieved.status_code, 200, achieved.data)
        self.assertEqual(achieved.data["status"], "achieved")
        self.assertIn("IOA met criterion", achieved.data["rationale"])

        discontinued = self.client.post(
            f"/api/v1/milestones/{milestone_id}/transition",
            {"status": "discontinued", "rationale": "Should not work"},
            format="json",
        )
        self.assertEqual(discontinued.status_code, 400)

    def test_05_supervisee_has_view_only_access_to_plan(self):
        rel = SupervisoryRelationship.objects.get(supervisee__email="alex@akevra.test")
        self.auth("jordan@akevra.test")
        created = self.client.post(
            f"/api/v1/relationships/{rel.id}/development-plans",
            self._plan_payload(),
            format="json",
        )
        self.assertEqual(created.status_code, 201, created.data)
        plan_id = created.data["id"]
        milestone_id = created.data["milestones"][0]["id"]

        self.client = APIClient()
        self.auth("alex@akevra.test")
        viewed = self.client.get(f"/api/v1/development-plans/{plan_id}")
        self.assertEqual(viewed.status_code, 200)
        self.assertFalse(viewed.data["can_manage"])
        self.assertEqual(viewed.data["summary"], "Initial IDP")

        listed = self.client.get(f"/api/v1/relationships/{rel.id}/development-plans")
        self.assertEqual(listed.status_code, 200)
        self.assertFalse(listed.data["can_manage"])

        denied_edit = self.client.patch(
            f"/api/v1/development-plans/{plan_id}",
            {"summary": "Supervisee should not be able to change this"},
            format="json",
        )
        self.assertEqual(denied_edit.status_code, 403)
        self.assertEqual(denied_edit.data["code"], "idp_view_only")

        denied_create = self.client.post(
            f"/api/v1/relationships/{rel.id}/development-plans",
            self._plan_payload("Should fail"),
            format="json",
        )
        self.assertEqual(denied_create.status_code, 403)
        self.assertEqual(denied_create.data["code"], "idp_view_only")

        denied_transition = self.client.post(
            f"/api/v1/milestones/{milestone_id}/transition",
            {"status": "achieved", "rationale": "Not allowed"},
            format="json",
        )
        self.assertEqual(denied_transition.status_code, 403)
        self.assertEqual(denied_transition.data["code"], "idp_view_only")

        unchanged = self.client.get(f"/api/v1/development-plans/{plan_id}")
        self.assertEqual(unchanged.data["summary"], "Initial IDP")
        self.assertEqual(unchanged.data["milestones"][0]["status"], "active")

    def test_06_dec_058_bcba_may_be_supervisee_without_fifth_role(self):
        self.assertEqual(
            set(MATRIX),
            {
                OrgRole.ADMINISTRATOR,
                OrgRole.CLINICAL_DIRECTOR,
                RelationshipRole.SUPERVISOR,
                RelationshipRole.SUPERVISEE,
            },
        )

        jordan = UserAccount.objects.get(email="jordan@akevra.test")
        director = UserAccount.objects.get(email="director@akevra.test")
        self.assertEqual(jordan.credential_type, "bcba")
        self.assertEqual(director.credential_type, "bcba")

        existing = SupervisoryRelationship.objects.get(supervisee=jordan)
        self.assertEqual(existing.supervision_track, SupervisionTrack.BCBA_PROFESSIONAL_DEVELOPMENT)
        self.assertEqual(
            existing.supervisee_purpose,
            SuperviseeAssignmentPurpose.STRUCTURED_PROFESSIONAL_DEVELOPMENT,
        )
        self.assertFalse(existing.is_compliance_bearing)

        self.auth("jordan@akevra.test")
        created = self.client.post(
            "/api/v1/relationships",
            {
                "supervisor_id": str(jordan.id),
                "supervisee_id": str(director.id),
                "supervision_track": SupervisionTrack.BCBA_PROFESSIONAL_DEVELOPMENT,
                "supervisee_purpose": SuperviseeAssignmentPurpose.CONSULTATION,
                "started_on": "2026-09-01",
            },
            format="json",
        )
        self.assertEqual(created.status_code, 201, created.data)
        self.assertEqual(created.data["supervisee"]["credential_type"], "bcba")
        self.assertEqual(created.data["supervisee_purpose"], "consultation")
        self.assertFalse(created.data["is_compliance_bearing"])
        self.assertEqual(created.data["your_role"], "supervisor")

        wrong_track = self.client.post(
            "/api/v1/relationships",
            {
                "supervisor_id": str(jordan.id),
                "supervisee_id": str(director.id),
                "supervision_track": SupervisionTrack.RBT_ONGOING,
                "started_on": "2026-09-02",
            },
            format="json",
        )
        self.assertEqual(wrong_track.status_code, 400)


class M3ReviewFixTests(TestCase):
    """Client M3 review (Sep 2026): pathway eligibility, intake credential, admin
    facilitation and relationship-scoped dashboard / competency data."""

    setUpTestData = SupervisionSetupAcceptanceTests.setUpTestData
    setUp = SupervisionSetupAcceptanceTests.setUp
    auth = SupervisionSetupAcceptanceTests.auth
    _plan_payload = SupervisionSetupAcceptanceTests._plan_payload

    def create(self, supervisor, supervisee, track, **extra):
        return self.client.post(
            "/api/v1/relationships",
            {
                "supervisor_id": str(supervisor.id),
                "supervisee_id": str(supervisee.id),
                "supervision_track": track,
                "started_on": "2026-09-01",
                **extra,
            },
            format="json",
        )

    def test_pathway_must_match_supervisee_credential(self):
        jordan = UserAccount.objects.get(email="jordan@akevra.test")
        alex = UserAccount.objects.get(email="alex@akevra.test")  # RBT
        self.auth("jordan@akevra.test")

        wrong = self.create(jordan, alex, SupervisionTrack.BCABA_ONGOING)
        self.assertEqual(wrong.status_code, 400)
        self.assertIn("RBT", str(wrong.data["detail"]))

        fieldwork = self.create(jordan, alex, SupervisionTrack.SUPERVISED_FIELDWORK, fieldwork_subtype="supervised")
        self.assertEqual(fieldwork.status_code, 201, fieldwork.data)

        parties = self.client.get("/api/v1/eligible-parties").data
        alex_row = next(p for p in parties["supervisees"] if p["email"] == "alex@akevra.test")
        self.assertEqual(alex_row["eligible_tracks"], ["rbt_ongoing_supervision", "supervised_fieldwork"])

    def test_intake_credential_always_comes_from_profile(self):
        rel = SupervisoryRelationship.objects.get(supervisee__email="alex@akevra.test")
        self.auth("jordan@akevra.test")
        intake = self.client.post(
            f"/api/v1/relationships/{rel.id}/intake",
            {"captured_on": "2026-09-01", "current_credential": "bcaba"},
            format="json",
        )
        self.assertEqual(intake.status_code, 201, intake.data)
        self.assertEqual(intake.data["current_credential"], "rbt")

    def test_intake_background_and_starting_point_are_structured(self):
        rel = SupervisoryRelationship.objects.get(supervisee__email="alex@akevra.test")
        self.auth("jordan@akevra.test")
        url = f"/api/v1/relationships/{rel.id}/intake"
        base = {"captured_on": "2026-09-01"}

        missing_other = self.client.post(url, {**base, "prior_aba_experience": "other"}, format="json")
        self.assertEqual(missing_other.status_code, 400)
        unknown = self.client.post(url, {**base, "starting_domains": ["juggling"]}, format="json")
        self.assertEqual(unknown.status_code, 400)

        intake = self.client.post(
            url,
            {
                **base,
                "prior_aba_experience": "rbt",
                "prior_aba_experience_other": "ignored unless Other is chosen",
                "current_context": ["new_organization", "other"],
                "current_context_other": "  Returning after leave  ",
                "starting_domains": ["data_collection", "clinical_documentation"],
                "further_assessment_needed": True,
            },
            format="json",
        )
        self.assertEqual(intake.status_code, 201, intake.data)
        self.assertEqual(intake.data["prior_aba_experience"], "rbt")
        self.assertEqual(intake.data["prior_aba_experience_other"], "")
        self.assertEqual(intake.data["current_context"], ["new_organization", "other"])
        self.assertEqual(intake.data["current_context_other"], "Returning after leave")
        self.assertEqual(intake.data["starting_domains"], ["data_collection", "clinical_documentation"])
        self.assertTrue(intake.data["further_assessment_needed"])

    def test_unanswered_assessment_flag_is_not_recorded_as_no(self):
        rel = SupervisoryRelationship.objects.get(supervisee__email="alex@akevra.test")
        self.auth("jordan@akevra.test")
        intake = self.client.post(f"/api/v1/relationships/{rel.id}/intake", {"captured_on": "2026-09-01"}, format="json")
        self.assertEqual(intake.status_code, 201, intake.data)
        self.assertIsNone(intake.data["further_assessment_needed"])

    def test_competencies_are_tagged_by_domain_and_pathway(self):
        rel = SupervisoryRelationship.objects.get(supervisee__email="alex@akevra.test")  # RBT ongoing
        self.auth("jordan@akevra.test")

        library = self.client.get("/api/v1/plan-library", {"track": rel.supervision_track})
        self.assertEqual(library.status_code, 200)
        keys = {item["key"] for item in library.data["competencies"]}
        self.assertIn("accurate_data_collection", keys)
        self.assertNotIn("interpreting_clinical_data", keys)  # analyst level only
        self.assertEqual(len(library.data["milestone_templates"]), 6)

        created = self.client.post(
            f"/api/v1/relationships/{rel.id}/development-plans",
            {
                "goals": [{"category": "data_collection", "title": "Collect data independently"}],
                "competencies": [
                    {"name": "  accurate data   collection ", "domain": "ethics"},
                    {"name": "Running group social skills", "domain": "skill_acquisition"},
                ],
            },
            format="json",
        )
        self.assertEqual(created.status_code, 201, created.data)
        library_item, custom = created.data["competencies"]
        self.assertEqual(library_item["competency_key"], "accurate_data_collection")
        self.assertEqual(library_item["domain"], "data_collection")  # the library's domain wins
        self.assertIn("rbt_ongoing_supervision", library_item["pathways"])
        self.assertEqual(custom["competency_key"], "")
        self.assertEqual(custom["domain"], "skill_acquisition")
        self.assertEqual(custom["pathways"], ["rbt_ongoing_supervision"])

    def test_milestones_from_editable_templates(self):
        rel = SupervisoryRelationship.objects.get(supervisee__email="alex@akevra.test")
        self.auth("jordan@akevra.test")
        url = f"/api/v1/relationships/{rel.id}/development-plans"
        goals = [{"category": "data_collection", "title": "Collect data independently"}]

        for bad in [{"title": "x", "template_key": "bacb_required"}, {"title": "x", "criterion_count": 0}, {"title": " "}]:
            self.assertEqual(self.client.post(url, {"goals": goals, "milestones": [bad]}, format="json").status_code, 400)

        created = self.client.post(
            url,
            {
                "goals": goals,
                "milestones": [
                    {
                        "template_key": "consecutive_observations",
                        "criterion_count": 4,
                        "title": "Demonstrates accurate ABC recording across 4 consecutive observations.",
                    },
                    {"title": "Presents one case at team meeting"},
                ],
            },
            format="json",
        )
        self.assertEqual(created.status_code, 201, created.data)
        templated, custom = created.data["milestones"]
        self.assertEqual(templated["template_key"], "consecutive_observations")
        self.assertEqual(templated["criterion_count"], 4)
        self.assertEqual(templated["title"], "Demonstrates accurate ABC recording across 4 consecutive observations.")
        self.assertEqual(custom["template_key"], "")
        self.assertIsNone(custom["criterion_count"])

    def test_competency_levels_use_fixed_scale(self):
        rel = SupervisoryRelationship.objects.get(supervisee__email="alex@akevra.test")
        self.auth("jordan@akevra.test")
        url = f"/api/v1/relationships/{rel.id}/development-plans"

        def plan(current, target, category="data_collection"):
            return {
                "goals": [{"category": category, "title": "Collect data independently"}],
                "competencies": [{"name": "Accurate data collection", "current_level": current, "target_level": target}],
            }

        for current, target in [
            ("emerging", "independent"),           # not on the scale
            ("developing", "needs_training"),      # not a valid target
            ("independent", "developing"),         # target below current
        ]:
            self.assertEqual(self.client.post(url, plan(current, target), format="json").status_code, 400)
        self.assertEqual(self.client.post(url, plan("developing", "independent", "juggling"), format="json").status_code, 400)

        # Target may equal current (maintenance / generalization goals)
        created = self.client.post(url, plan("independent", "independent"), format="json")
        self.assertEqual(created.status_code, 201, created.data)
        self.assertEqual(created.data["goals"][0]["category"], "data_collection")

    def test_administrator_facilitates_without_becoming_a_party(self):
        morgan = UserAccount.objects.get(email="morgan@akevra.test")
        casey = UserAccount.objects.get(email="casey@akevra.test")
        alex = UserAccount.objects.get(email="alex@akevra.test")
        self.auth("settings.admin@akevra.test")

        created = self.create(morgan, casey, SupervisionTrack.BCABA_ONGOING)
        self.assertEqual(created.status_code, 201, created.data)
        self.assertIsNone(created.data["your_role"])
        self.assertFalse(created.data["permissions"]["intake.manage"])

        # no access to the record afterwards, and a Supervisor must still be a BCBA
        self.assertEqual(self.client.get(f"/api/v1/relationships/{created.data['id']}").status_code, 403)
        self.assertEqual(self.client.get("/api/v1/relationships").data, [])
        self.assertEqual(self.create(alex, casey, SupervisionTrack.BCABA_ONGOING).status_code, 400)

    def test_dashboard_summary_is_scoped_to_own_relationships(self):
        from datetime import timedelta

        from django.utils import timezone

        from apps.supervision.models import Appointment

        rel = SupervisoryRelationship.objects.get(supervisee__email="alex@akevra.test")
        start = timezone.now() + timedelta(days=1)
        Appointment.objects.create(organization=rel.organization, relationship=rel, starts_at=start, ends_at=start + timedelta(hours=1))

        self.auth("jordan@akevra.test")
        mine = self.client.get("/api/v1/dashboard/summary").data
        self.assertEqual([s["with"] for s in mine["upcoming_sessions"]], ["Alex Rivera"])
        self.assertEqual(mine["cycle"]["sessions_logged"], 0)

        self.client = APIClient()
        self.auth("casey@akevra.test")
        self.assertEqual(self.client.get("/api/v1/dashboard/summary").data["upcoming_sessions"], [])

    def test_competencies_listed_per_relationship_view_only_for_supervisee(self):
        rel = SupervisoryRelationship.objects.get(supervisee__email="alex@akevra.test")
        self.auth("jordan@akevra.test")
        self.client.post(f"/api/v1/relationships/{rel.id}/development-plans", self._plan_payload(), format="json")

        self.client = APIClient()
        self.auth("alex@akevra.test")
        rows = self.client.get("/api/v1/competencies").data
        self.assertEqual(len(rows), 1)
        self.assertFalse(rows[0]["can_manage"])
        self.assertEqual(rows[0]["plan"]["competencies"][0]["name"], "Session documentation")

        self.client = APIClient()
        self.auth("casey@akevra.test")
        self.assertEqual(self.client.get("/api/v1/competencies").data, [])


class DashboardLoadingTests(TestCase):
    """Client M3 retest (Sep 2026): dashboard widgets stuck on "Loading…". The dashboard
    fires several requests at once with the same session; they must not queue behind
    each other, and their cost must not grow with the number of relationships."""

    setUpTestData = SupervisionSetupAcceptanceTests.setUpTestData
    setUp = SupervisionSetupAcceptanceTests.setUp
    auth = SupervisionSetupAcceptanceTests.auth

    DASHBOARD_ENDPOINTS = (
        "/api/v1/auth/me",
        "/api/v1/relationships",
        "/api/v1/dashboard/summary",
        "/api/v1/competencies",
    )

    def test_query_count_does_not_grow_with_relationships(self):
        from django.db import connection
        from django.test.utils import CaptureQueriesContext

        self.auth("director@akevra.test")
        before = {}
        for path in self.DASHBOARD_ENDPOINTS:
            with CaptureQueriesContext(connection) as queries:
                self.assertEqual(self.client.get(path).status_code, 200, path)
            before[path] = len(queries)

        director = UserAccount.objects.get(email="director@akevra.test")
        supervisor = UserAccount.objects.filter(organization=director.organization, credential_type="bcba").first()
        for supervisee in UserAccount.objects.filter(organization=director.organization, credential_type="rbt")[:1]:
            for _ in range(5):
                SupervisoryRelationship.objects.create(
                    organization=director.organization,
                    supervisor=supervisor,
                    supervisee=supervisee,
                    supervision_track=SupervisionTrack.RBT_ONGOING,
                    started_on="2026-09-01",
                )

        for path in ("/api/v1/relationships", "/api/v1/competencies"):
            with CaptureQueriesContext(connection) as queries:
                self.assertEqual(self.client.get(path).status_code, 200, path)
            self.assertEqual(len(queries), before[path], path)

    def test_session_activity_is_still_recorded(self):
        from datetime import timedelta

        from django.utils import timezone

        from apps.accounts.models import AuthSession

        self.auth("jordan@akevra.test")
        session = AuthSession.objects.filter(revoked_at__isnull=True).latest("created_at")
        stale = timezone.now() - timedelta(minutes=2)
        AuthSession.objects.filter(pk=session.pk).update(last_seen_at=stale)

        self.assertEqual(self.client.get("/api/v1/dashboard/summary").status_code, 200)
        session.refresh_from_db()
        self.assertGreater(session.last_seen_at, stale)


class SessionTouchConcurrencyTests(TransactionTestCase):
    """Committed rows, so a second connection can see and lock the session row."""

    def setUp(self):
        from django.core.management import call_command

        call_command("seed_sprint0", verbosity=0)
        self.client = APIClient()

    auth = SupervisionSetupAcceptanceTests.auth

    def test_session_touch_skips_a_row_another_request_is_updating(self):
        import threading
        import time
        from datetime import timedelta

        from django.db import connections, transaction
        from django.utils import timezone

        from apps.accounts.models import AuthSession
        from apps.core import rls

        self.auth("jordan@akevra.test")
        with rls.rls_bypass():
            session = AuthSession.objects.filter(revoked_at__isnull=True).latest("created_at")
            stale = timezone.now() - timedelta(minutes=2)
            AuthSession.objects.filter(pk=session.pk).update(last_seen_at=stale)
            session.refresh_from_db()

        # A second connection stands in for a concurrent request holding the row lock.
        locked, release = threading.Event(), threading.Event()

        def hold_lock():
            other = connections.create_connection("default")
            try:
                other.set_autocommit(False)
                with other.cursor() as cursor:
                    cursor.execute("SELECT id FROM auth_session WHERE id = %s FOR UPDATE", [session.pk])
                    self.assertIsNotNone(cursor.fetchone())
                    locked.set()
                    release.wait(5)
                other.rollback()
            finally:
                other.close()

        holder = threading.Thread(target=hold_lock)
        holder.start()
        try:
            self.assertTrue(locked.wait(5))
            started = time.monotonic()
            with transaction.atomic(), rls.rls_bypass():
                session.touch()
            elapsed = time.monotonic() - started
        finally:
            release.set()
            holder.join()
        self.assertLess(elapsed, 2, "touch() waited for another request's lock on the session row")
