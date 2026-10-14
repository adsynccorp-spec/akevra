from datetime import date

from django.core.management import call_command
from django.test import TestCase
from rest_framework.test import APIClient

from apps.accounts.models import UserAccount
from apps.core.tests import PASSWORD
from apps.supervision.models import SupervisionTrack, SupervisoryRelationship
from apps.supervision.services import create_development_plan


class CompetencyAssessmentTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        call_command("seed_sprint0", verbosity=0)
        cls.jordan = UserAccount.objects.get(email="jordan@akevra.test")
        cls.alex = UserAccount.objects.get(email="alex@akevra.test")
        cls.casey = UserAccount.objects.get(email="casey@akevra.test")
        cls.rbt = SupervisoryRelationship.objects.get(supervisor=cls.jordan, supervisee=cls.alex)
        cls.plan = create_development_plan(
            organization=cls.rbt.organization, relationship=cls.rbt,
            goals=[{"title": "Accurate measurement", "category": "data_collection"}],
            competencies=[{"name": "Session documentation"}, {"name": "Data collection"}],
        )
        other = SupervisoryRelationship.objects.create(
            organization=cls.jordan.organization, supervisor=cls.jordan, supervisee=cls.casey,
            supervision_track=SupervisionTrack.BCABA_ONGOING, started_on=date(2026, 9, 1),
        )
        cls.foreign = create_development_plan(organization=other.organization, relationship=other,
                                              competencies=[{"name": "Someone else's"}]).competencies.first()

    def as_user(self, email):
        client = APIClient()
        token = client.post("/api/v1/auth/login", {"email": email, "password": PASSWORD}, format="json").data["token"]
        client.credentials(HTTP_AUTHORIZATION=f"Bearer {token}")
        return client

    def test_assessment_workflow_and_visibility(self):
        jordan, alex = self.as_user("jordan@akevra.test"), self.as_user("alex@akevra.test")
        docs, data = self.plan.competencies.order_by("sort_order")
        goal = self.plan.goals.first()
        url = f"/api/v1/relationships/{self.rbt.id}/assessments"

        self.assertEqual(alex.post(url, {"plan_competency_id": str(docs.id)}, format="json").status_code, 403)
        self.assertEqual(jordan.post(url, {"plan_competency_id": str(self.foreign.id)}, format="json").status_code, 400)
        draft = jordan.post(url, {"plan_competency_id": str(docs.id), "related_goal_id": str(goal.id), "assessed_on": "2026-10-12"}, format="json")
        self.assertEqual(draft.status_code, 201, draft.data)
        self.assertEqual((draft.data["status"], draft.data["competency"]["name"]), ("draft", "Session documentation"))
        aid = draft.data["id"]

        self.assertEqual(alex.get(url).data["assessments"], [])
        self.assertEqual(alex.get(f"/api/v1/assessments/{aid}").status_code, 404)
        self.assertEqual(jordan.post(f"/api/v1/assessments/{aid}/submit").status_code, 400)  # no rating yet
        jordan.patch(f"/api/v1/assessments/{aid}", {"rating": "independent"}, format="json")
        self.assertEqual(jordan.post(f"/api/v1/assessments/{aid}/submit").data["status"], "ready_for_review")
        self.assertEqual(jordan.post(f"/api/v1/assessments/{aid}/return").data["status"], "draft")
        self.assertEqual(jordan.post(f"/api/v1/assessments/{aid}/finalize").status_code, 400)  # no evidence
        jordan.patch(f"/api/v1/assessments/{aid}", {"documented_content": "Notes complete and timely across 5 sessions."}, format="json")
        self.assertEqual(alex.post(f"/api/v1/assessments/{aid}/finalize").status_code, 403)
        official = jordan.post(f"/api/v1/assessments/{aid}/finalize")
        self.assertEqual(official.data["status"], "official")
        self.assertIsNotNone(official.data["finalized_at"])
        self.assertEqual(jordan.patch(f"/api/v1/assessments/{aid}", {"rating": "developing"}, format="json").status_code, 400)

        second = jordan.post(url, {"plan_competency_id": str(data.id), "rating": "needs_training"}, format="json").data
        listed = alex.get(url).data
        self.assertEqual([a["id"] for a in listed["assessments"]], [aid])  # drafts stay hidden
        self.assertFalse(listed["can_manage"])
        self.assertEqual(listed["summary"], {"total_assessed": 1, "proficient": 1, "developing": 0, "needs_improvement": 0})

        jordan.patch(f"/api/v1/assessments/{second['id']}", {"documented_content": "Missed IOA twice."}, format="json")
        jordan.post(f"/api/v1/assessments/{second['id']}/finalize")
        summary = jordan.get(url).data["summary"]
        self.assertEqual((summary["total_assessed"], summary["needs_improvement"]), (2, 1))
        director = self.as_user("director@akevra.test")
        self.assertEqual(len(director.get(url).data["assessments"]), 2)
