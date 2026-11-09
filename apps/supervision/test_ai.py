"""M7 Part B: AI Teaching Assistant, provenance and disposition (acceptance criteria B1-B5).
Runs on the no-API-key path (the deterministic akevra-rules/1.0 generator); the API path is checked
with a mocked HTTPS response."""

import io
import json
from datetime import date
from unittest import mock

from django.core.exceptions import ValidationError
from django.core.management import call_command
from django.test import TestCase
from rest_framework.test import APIClient

from apps.accounts.models import UserAccount
from apps.audit.models import AuditEvent
from apps.core.models import HardDeleteNotAllowed
from apps.core.tests import PASSWORD
from apps.supervision import ai
from apps.supervision.models import (
    AISuggestion,
    AssessmentStatus,
    CompetencyAssessment,
    DevelopmentPlan,
    SupervisoryRelationship,
)
from apps.supervision.services import create_development_plan


class AITeachingAssistantTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        call_command("seed_sprint0", verbosity=0)
        cls.jordan = UserAccount.objects.get(email="jordan@akevra.test")
        cls.alex = UserAccount.objects.get(email="alex@akevra.test")
        cls.rbt = SupervisoryRelationship.objects.get(supervisor=cls.jordan, supervisee=cls.alex)
        cls.plan = create_development_plan(
            organization=cls.rbt.organization, relationship=cls.rbt,
            goals=[{"title": "Accurate measurement", "category": "data_collection"}],
            competencies=[
                {"name": "Session documentation", "current_level": "developing", "target_level": "independent"},
                {"name": "Data collection", "current_level": "performs_with_support", "target_level": "independent"},
            ],
        )
        docs = cls.plan.competencies.get(name="Session documentation")
        CompetencyAssessment.objects.create(
            organization=cls.rbt.organization, relationship=cls.rbt, assessed_on=date(2026, 10, 12),
            rating="needs_training", status=AssessmentStatus.OFFICIAL, plan_competency=docs,
            competency_name=docs.name, documented_content="Notes late in 3 of 5 sessions.", assessed_by=cls.jordan,
        )

    def setUp(self):
        patcher = mock.patch.object(ai, "_api_key", return_value="")  # the no-API-key path
        patcher.start()
        self.addCleanup(patcher.stop)

    def as_user(self, email):
        client = APIClient()
        token = client.post("/api/v1/auth/login", {"email": email, "password": PASSWORD}, format="json").data["token"]
        client.credentials(HTTP_AUTHORIZATION=f"Bearer {token}")
        return client

    def generate(self, client=None):
        client = client or self.as_user("jordan@akevra.test")
        response = client.post(f"/api/v1/relationships/{self.rbt.id}/ai-suggestions/generate")
        self.assertEqual(response.status_code, 201, response.data)
        return response.data["suggestions"]

    def plan_state(self):
        plans = DevelopmentPlan.objects.filter(relationship=self.rbt)
        current = plans.get(is_current=True)
        return plans.count(), current.version_number, sorted(current.goals.values_list("title", flat=True))

    def dispose(self, client, suggestion_id, **body):
        return client.post(f"/api/v1/ai-suggestions/{suggestion_id}/disposition", body, format="json")

    # --- B1: inert until dispositioned -------------------------------------------------------
    def test_b1_suggestions_are_inert_until_a_supervisor_dispositions_them(self):
        before = self.plan_state()
        assessments = list(CompetencyAssessment.objects.filter(relationship=self.rbt).values())
        suggestions = self.generate()
        self.assertTrue(2 <= len(suggestions) <= 4)
        self.assertTrue(all(s["disposition"] == "pending" and s["is_current"] for s in suggestions))
        self.assertEqual(self.plan_state(), before)  # nothing written to the plan
        self.assertEqual(list(CompetencyAssessment.objects.filter(relationship=self.rbt).values()), assessments)
        # The rule generator turned the low official rating into a suggested goal
        titles = [s["content"]["title"] for s in suggestions]
        self.assertIn("Raise Session documentation from Needs Training to Independent", titles)

        jordan = self.as_user("jordan@akevra.test")
        accepted = self.dispose(jordan, suggestions[0]["id"], action="accept")
        self.assertEqual(accepted.status_code, 200, accepted.data)
        count, version, goals = self.plan_state()
        self.assertEqual((count, version), (before[0] + 1, before[1] + 1))  # a NEW plan version
        self.assertIn(suggestions[0]["content"]["title"], goals)

    # --- B2: immutable identifier, prompt, model, timestamp ----------------------------------
    def test_b2_provenance_is_kept_and_immutable(self):
        suggestion = self.generate()[0]
        jordan = self.as_user("jordan@akevra.test")
        detail = jordan.get(f"/api/v1/ai-suggestions/{suggestion['id']}").data
        self.assertEqual(detail["model_name"], ai.RULES_GENERATOR)
        self.assertEqual(detail["generator"], "rules")
        self.assertTrue(detail["identifier"] and detail["created_at"] and detail["context_hash"])
        prompt = json.loads(detail["prompt"])
        self.assertEqual(prompt["generator"], ai.RULES_GENERATOR)
        self.assertEqual(prompt["context"]["pathway"], "rbt_ongoing_supervision")
        for name in ("Alex", "Rivera", "Jordan", "Lee", "alex@"):  # de-identified
            self.assertNotIn(name, detail["prompt"])

        row = AISuggestion.objects.get(pk=suggestion["id"])
        for field, value in (("prompt", "changed"), ("model_name", "other-model"), ("explanation", "x"),
                             ("content", {"title": "rewritten"})):
            fresh = AISuggestion.objects.get(pk=row.pk)
            setattr(fresh, field, value)
            with self.assertRaises(ValidationError):
                fresh.save()
        fresh = AISuggestion.objects.get(pk=row.pk)
        fresh.suggestion_identifier = row.id
        with self.assertRaises(ValidationError):
            fresh.save()
        # Dispositioning leaves the generated provenance untouched
        self.dispose(jordan, suggestion["id"], action="accept")
        after = AISuggestion.objects.get(pk=row.pk)
        self.assertEqual((after.suggestion_identifier, after.prompt, after.model_name, after.content, after.created_at),
                         (row.suggestion_identifier, row.prompt, row.model_name, row.content, row.created_at))

    # --- B3: Accept / Edit-and-Accept / Reject ------------------------------------------------
    def test_b3_edit_and_accept_keeps_both_texts_with_attribution(self):
        suggestion = self.generate()[0]
        jordan = self.as_user("jordan@akevra.test")
        unchanged = self.dispose(jordan, suggestion["id"], action="edit_accept", final_content={
            "category": suggestion["content"]["category"], "title": suggestion["content"]["title"],
            "description": suggestion["content"]["description"]})
        self.assertEqual(unchanged.status_code, 400)  # nothing edited: that's an Accept
        self.assertEqual(self.dispose(jordan, suggestion["id"], action="edit_accept", final_content={"title": ""}).status_code, 400)

        final = {"category": "clinical_documentation", "title": "Complete session notes within 24 hours in 9 of 10 sessions",
                 "description": "Checked weekly by the supervisor."}
        response = self.dispose(jordan, suggestion["id"], action="edit_accept", final_content=final, note="Tightened the criterion")
        self.assertEqual(response.status_code, 200, response.data)
        data = response.data
        self.assertEqual(data["disposition"], "edited_accepted")
        self.assertEqual(data["content"], suggestion["content"])  # the original AI text is kept
        self.assertEqual(data["final_content"], final)            # beside the Supervisor's text
        self.assertEqual(data["attribution"]["original"]["author"], ai.RULES_GENERATOR)
        self.assertEqual(data["attribution"]["final"]["author"]["name"], self.jordan.full_name)
        self.assertTrue(data["attribution"]["final"]["edited"])
        self.assertEqual([e["action"] for e in data["events"]], ["generated", "edited_accepted"])
        self.assertEqual(data["events"][-1]["actor"]["name"], self.jordan.full_name)
        goal = DevelopmentPlan.objects.get(relationship=self.rbt, is_current=True).goals.get(ai_suggestion_id=suggestion["id"])
        self.assertEqual(goal.title, final["title"])  # the official record carries the Supervisor's text
        self.assertTrue(AuditEvent.objects.filter(action="ai_suggestion.edit_accept", entity_id=suggestion["id"]).exists())

    def test_b3_reject_requires_a_reason_and_changes_nothing(self):
        suggestion = self.generate()[0]
        jordan = self.as_user("jordan@akevra.test")
        before = self.plan_state()
        self.assertEqual(self.dispose(jordan, suggestion["id"], action="reject").status_code, 400)
        self.assertEqual(self.dispose(jordan, suggestion["id"], action="maybe").status_code, 400)
        rejected = self.dispose(jordan, suggestion["id"], action="reject", note="Already covered in supervision")
        self.assertEqual(rejected.status_code, 200, rejected.data)
        self.assertEqual((rejected.data["disposition"], rejected.data["disposition_note"]), ("rejected", "Already covered in supervision"))
        self.assertEqual(self.plan_state(), before)
        self.assertEqual(self.dispose(jordan, suggestion["id"], action="accept").status_code, 400)  # decided once

    # --- B4: rejected and superseded retained; stale never current ---------------------------
    def test_b4_rejected_and_superseded_are_retained_and_stale_cannot_be_accepted(self):
        jordan = self.as_user("jordan@akevra.test")
        first = self.generate(jordan)
        self.dispose(jordan, first[0]["id"], action="reject", note="Not a priority")
        second = self.generate(jordan)  # supersedes the rest of the first batch

        listed = jordan.get(f"/api/v1/relationships/{self.rbt.id}/ai-suggestions").data
        by_id = {s["id"]: s for s in listed["suggestions"]}
        self.assertEqual(len(by_id), len(first) + len(second))  # nothing removed
        self.assertEqual(by_id[first[0]["id"]]["disposition"], "rejected")
        self.assertEqual(by_id[first[0]["id"]]["content"], first[0]["content"])  # retained in full
        for old in first[1:]:
            row = by_id[old["id"]]
            self.assertEqual(row["disposition"], "superseded")
            self.assertTrue(row["stale"])
            self.assertFalse(row["is_current"])
            self.assertIn("Superseded", row["status_label"])
            self.assertEqual(row["superseded_by"], second[0]["identifier"])
            self.assertEqual(row["events"][-1]["action"], "superseded")
        self.assertEqual(self.dispose(jordan, first[1]["id"], action="accept").status_code, 409)
        self.assertEqual(listed["counts"]["superseded"], len(first) - 1)
        self.assertEqual(listed["counts"]["rejected"], 1)

        # Accepting one suggestion doesn't make its siblings from the same request stale
        self.assertEqual(self.dispose(jordan, second[0]["id"], action="accept").status_code, 200)
        sibling = jordan.get(f"/api/v1/ai-suggestions/{second[1]['id']}").data
        self.assertFalse(sibling["stale"])

        # A record the suggestions came from changes: the pending ones are stale, never shown as current
        data_collection = DevelopmentPlan.objects.get(relationship=self.rbt, is_current=True).competencies.get(name="Data collection")
        CompetencyAssessment.objects.create(
            organization=self.rbt.organization, relationship=self.rbt, assessed_on=date(2026, 11, 2),
            rating="independent", status=AssessmentStatus.OFFICIAL, plan_competency=data_collection,
            competency_name=data_collection.name, assessed_by=self.jordan,
        )
        stale = jordan.get(f"/api/v1/ai-suggestions/{second[1]['id']}").data
        self.assertTrue(stale["stale"])
        self.assertFalse(stale["is_current"])
        self.assertIn("Out of date", stale["status_label"])
        page = jordan.get("/api/v1/ai-suggestions").data
        self.assertEqual(page["counts"]["awaiting_review"], 0)
        self.assertEqual(page["counts"]["stale"], len(second) - 1)
        blocked = self.dispose(jordan, second[1]["id"], action="accept")
        self.assertEqual((blocked.status_code, blocked.data["code"]), (409, "suggestion_stale"))
        # A stale suggestion can still be rejected to close it
        self.assertEqual(self.dispose(jordan, second[1]["id"], action="reject", note="Out of date").status_code, 200)

        # Never deleted: no API delete, and the model refuses
        self.assertEqual(jordan.delete(f"/api/v1/ai-suggestions/{first[0]['id']}").status_code, 405)
        with self.assertRaises(HardDeleteNotAllowed):
            AISuggestion.objects.get(pk=first[0]["id"]).delete()
        with self.assertRaises(HardDeleteNotAllowed):
            AISuggestion.objects.filter(relationship=self.rbt).delete()

    # --- B5: official content resolves back to its suggestion, across plan versions ----------
    def test_b5_adopted_goal_resolves_to_its_suggestion_through_later_versions(self):
        jordan = self.as_user("jordan@akevra.test")
        suggestion = self.generate(jordan)[0]
        self.dispose(jordan, suggestion["id"], action="accept", note="Good fit")
        plans_url = f"/api/v1/relationships/{self.rbt.id}/development-plans"
        current = next(p for p in jordan.get(plans_url).data["plans"] if p["is_current"])
        goal = next(g for g in current["goals"] if g["ai_suggestion"])
        self.assertEqual(goal["ai_suggestion"], {"id": suggestion["id"], "identifier": suggestion["identifier"],
                                                 "model_name": ai.RULES_GENERATOR, "disposition": "accepted"})

        # A later edit that leaves goals alone carries the link forward
        revised = jordan.patch(f"/api/v1/development-plans/{current['id']}", {"summary": "Updated focus"}, format="json")
        self.assertEqual(revised.status_code, 200, revised.data)
        carried = next(g for g in revised.data["goals"] if g["title"] == goal["title"])
        self.assertEqual(carried["ai_suggestion"]["identifier"], suggestion["identifier"])

        # So does a full edit that sends the goals back (the plan editor sends ai_suggestion_id)
        goals = [{"title": g["title"], "category": g["category"], "description": g["description"], "status": g["status"],
                  "ai_suggestion_id": g["ai_suggestion"]["id"] if g["ai_suggestion"] else None} for g in revised.data["goals"]]
        edited = jordan.patch(f"/api/v1/development-plans/{revised.data['id']}", {"goals": goals}, format="json")
        self.assertEqual(edited.status_code, 200, edited.data)
        self.assertEqual(next(g for g in edited.data["goals"] if g["title"] == goal["title"])["ai_suggestion"]["id"], suggestion["id"])

        # The suggestion resolves to every plan version the goal is in, plus its history
        detail = jordan.get(f"/api/v1/ai-suggestions/{suggestion['id']}").data
        self.assertEqual([g["plan_version"] for g in detail["adopted_goals"]], [2, 3, 4])
        self.assertEqual([e["action"] for e in detail["events"]], ["generated", "accepted"])

        # A goal can't be linked to a suggestion nobody accepted
        pending = AISuggestion.objects.filter(relationship=self.rbt, disposition="pending").first()
        bad = jordan.patch(f"/api/v1/development-plans/{edited.data['id']}",
                           {"goals": [{"title": "Sneaky", "ai_suggestion_id": str(pending.id)}]}, format="json")
        self.assertEqual(bad.status_code, 400)

    # --- RBAC -------------------------------------------------------------------------------
    def test_rbac_supervisee_and_other_organization_are_denied(self):
        jordan = self.as_user("jordan@akevra.test")
        suggestions = self.generate(jordan)
        adopted, pending = suggestions[0], suggestions[1]
        self.dispose(jordan, adopted["id"], action="accept")

        alex = self.as_user("alex@akevra.test")  # the Supervisee
        self.assertEqual(alex.post(f"/api/v1/relationships/{self.rbt.id}/ai-suggestions/generate").status_code, 403)
        self.assertEqual(self.dispose(alex, pending["id"], action="accept").status_code, 404)
        self.assertEqual(self.dispose(alex, adopted["id"], action="reject", note="x").status_code, 403)
        listed = alex.get(f"/api/v1/relationships/{self.rbt.id}/ai-suggestions").data
        self.assertEqual([s["id"] for s in listed["suggestions"]], [adopted["id"]])  # adopted provenance only
        self.assertFalse(listed["can_manage"])
        self.assertEqual(alex.get(f"/api/v1/ai-suggestions/{pending['id']}").status_code, 404)
        self.assertEqual(alex.get(f"/api/v1/ai-suggestions/{adopted['id']}").status_code, 200)
        self.assertEqual([s["id"] for s in alex.get("/api/v1/ai-suggestions").data["suggestions"]], [adopted["id"]])

        director = self.as_user("director@akevra.test")  # oversight: sees all, decides nothing
        self.assertEqual(len(director.get(f"/api/v1/relationships/{self.rbt.id}/ai-suggestions").data["suggestions"]), len(suggestions))
        self.assertEqual(self.dispose(director, pending["id"], action="accept").status_code, 403)

        outsider = self.as_user("summit.supervisor@akevra.test")  # another organization
        self.assertEqual(outsider.get(f"/api/v1/relationships/{self.rbt.id}/ai-suggestions").status_code, 404)
        self.assertEqual(outsider.post(f"/api/v1/relationships/{self.rbt.id}/ai-suggestions/generate").status_code, 404)
        self.assertEqual(outsider.get(f"/api/v1/ai-suggestions/{adopted['id']}").status_code, 404)
        self.assertEqual(self.dispose(outsider, pending["id"], action="accept").status_code, 404)
        self.assertEqual(outsider.get("/api/v1/ai-suggestions").data["suggestions"], [])
        self.assertEqual(AISuggestion.objects.get(pk=pending["id"]).disposition, "pending")

    # --- Generator selection ------------------------------------------------------------------
    def test_no_api_key_uses_the_rule_generator(self):
        page = self.as_user("jordan@akevra.test").get("/api/v1/ai-suggestions").data
        self.assertEqual(page["generator"], {"kind": "rules", "name": ai.RULES_GENERATOR, "ai_configured": False})

    def test_api_path_stores_prompt_and_returned_model_and_falls_back_on_error(self):
        reply = {"model": "claude-sonnet-5-5", "stop_reason": "end_turn", "content": [{"type": "text", "text": json.dumps({
            "suggestions": [
                {"category": "clinical_documentation", "title": "Notes within 24 hours", "description": "d",
                 "evidence": ["Official rating: Needs Training"], "confidence": "high", "explanation": "e"},
                {"category": "nonsense", "title": "Second goal", "description": "d", "evidence": [], "confidence": "?",
                 "explanation": "e"},
            ]})}]}
        response = mock.MagicMock()
        response.__enter__.return_value = io.BytesIO(json.dumps(reply).encode())
        with mock.patch.object(ai, "_api_key", return_value="test-key"), \
                mock.patch("urllib.request.urlopen", return_value=response) as urlopen:
            created = ai.generate(self.rbt, self.jordan)
        sent = urlopen.call_args.args[0]
        self.assertEqual(sent.full_url, ai.API_URL)
        self.assertEqual(sent.get_header("X-api-key"), "test-key")
        self.assertEqual(sent.get_header("Anthropic-version"), "2023-06-01")
        self.assertEqual(json.loads(sent.data)["messages"][0]["content"], created[0].prompt)  # the exact prompt
        self.assertEqual([s.model_name for s in created], ["claude-sonnet-5-5"] * 2)
        self.assertEqual((created[1].content["category"], created[1].content["confidence"]), ("other", "low"))

        with mock.patch.object(ai, "_api_key", return_value="test-key"), \
                mock.patch("urllib.request.urlopen", side_effect=OSError("network down")):
            fallback = ai.generate(self.rbt, self.jordan)
        self.assertEqual({s.model_name for s in fallback}, {ai.RULES_GENERATOR})
