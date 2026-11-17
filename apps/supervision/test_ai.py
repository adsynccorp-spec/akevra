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
        data = cls.plan.competencies.get(name="Data collection")
        CompetencyAssessment.objects.create(
            organization=cls.rbt.organization, relationship=cls.rbt, assessed_on=date(2026, 10, 12),
            rating="developing", status=AssessmentStatus.OFFICIAL, plan_competency=data,
            competency_name=data.name, documented_content="Prompting inconsistent.", assessed_by=cls.jordan,
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

    def generate(self, client=None, mode="justification"):
        # Disposition tests use justification mode (shown directly); teaching mode is covered separately
        client = client or self.as_user("jordan@akevra.test")
        body = {"mode": mode} if mode else {}
        response = client.post(f"/api/v1/relationships/{self.rbt.id}/ai-suggestions/generate", body, format="json")
        self.assertEqual(response.status_code, 201, response.data)
        # Development goals first (the disposition tests adopt them); noted-only items after
        return sorted(response.data["suggestions"], key=lambda s: not s["is_goal"])

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
        self.assertIn("Raise Session documentation from Not Yet Demonstrated to Proficient", titles)

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
        self.assertEqual(page["generator"], {"kind": "rules", "name": ai.RULES_GENERATOR, "ai_configured": False, "ai_enabled": False})

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
        self.enable_ai()
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
        # REQ-031: a bare string becomes evidence with category and strength; confidence follows strength, not the model
        self.assertEqual(created[0].content["evidence"], [{"text": "Official rating: Needs Training", "category": "other", "strength": "limited"}])
        self.assertEqual(created[0].content["confidence"], "low")

        with mock.patch.object(ai, "_api_key", return_value="test-key"), \
                mock.patch("urllib.request.urlopen", side_effect=OSError("network down")):
            fallback = ai.generate(self.rbt, self.jordan)
        self.assertEqual({s.model_name for s in fallback}, {ai.RULES_GENERATOR})

    # --- helpers for the opt-in AI path ------------------------------------------------------
    def enable_ai(self, on=True):
        org = self.rbt.organization
        org.ai_enabled = on
        org.save(update_fields=["ai_enabled", "updated_at"])

    def mocked_reply(self, suggestions):
        reply = {"model": "claude-sonnet-5-5", "stop_reason": "end_turn",
                 "content": [{"type": "text", "text": json.dumps({"suggestions": suggestions})}]}
        response = mock.MagicMock()
        response.__enter__.return_value = io.BytesIO(json.dumps(reply).encode())
        return response

    TWO = [
        {"category": "clinical_documentation", "title": "Notes within 24 hours", "description": "d",
         "evidence": [{"text": "Official rating: Needs Training", "category": "official_assessment", "strength": "strong"}],
         "assumptions": ["a"], "limitations": ["l"], "alternatives": [], "teaching_question": "q?", "explanation": "e"},
        {"category": "data_collection", "title": "Second goal", "description": "d",
         "evidence": [{"text": "x", "category": "compliance_record", "strength": "moderate"},
                      {"text": "y", "category": "nope", "strength": "??"}], "explanation": "e"},
    ]

    # --- Opt-in: nothing goes to the AI provider unless the organization turned AI on ---------
    def test_ai_is_off_until_an_administrator_turns_it_on(self):
        self.assertFalse(self.rbt.organization.ai_enabled)  # default off
        with mock.patch.object(ai, "_api_key", return_value="test-key"), mock.patch("urllib.request.urlopen") as urlopen:
            created = ai.generate(self.rbt, self.jordan)
            info = ai.generator_info(self.rbt.organization)
        urlopen.assert_not_called()
        self.assertEqual({s.model_name for s in created}, {ai.RULES_GENERATOR})
        self.assertEqual(info, {"kind": "rules", "name": ai.RULES_GENERATOR, "ai_configured": True, "ai_enabled": False})

        jordan = self.as_user("jordan@akevra.test")
        self.assertEqual(jordan.patch("/api/v1/organization/settings", {"ai_enabled": True}, format="json").status_code, 403)
        admin = APIClient()
        token = admin.post("/api/v1/auth/login", {"email": "settings.admin@akevra.test", "password": PASSWORD}, format="json").data["token"]
        admin.credentials(HTTP_AUTHORIZATION=f"Bearer {token}")
        with mock.patch.object(ai, "_api_key", return_value="test-key"):
            turned_on = admin.patch("/api/v1/organization/settings", {"ai_enabled": True}, format="json").data
        self.assertTrue(turned_on["ai_enabled"])
        self.assertEqual(turned_on["ai_generator"]["kind"], "ai")
        self.assertTrue(AuditEvent.objects.filter(action="organization.settings", after={"ai_enabled": True}).exists())
        self.rbt.organization.refresh_from_db()
        with mock.patch.object(ai, "_api_key", return_value="test-key"), \
                mock.patch("urllib.request.urlopen", return_value=self.mocked_reply(self.TWO)) as urlopen:
            created = ai.generate(self.rbt, self.jordan)
        urlopen.assert_called_once()
        self.assertEqual({s.model_name for s in created}, {"claude-sonnet-5-5"})

    # --- Redaction: identifiers typed into free text never reach the provider ------------------
    def test_identifiers_in_free_text_are_redacted_before_sending(self):
        self.enable_ai()
        from apps.supervision.services import revise_development_plan

        revise_development_plan(self.plan, goals=[
            {"title": "Review notes with Alex Rivera (alex@akevra.test, 555-123-4567, cert 1-01-11007, MRN 4829301)",
             "category": "clinical_documentation"}])
        with mock.patch.object(ai, "_api_key", return_value="test-key"), \
                mock.patch("urllib.request.urlopen", return_value=self.mocked_reply(self.TWO)) as urlopen:
            created = ai.generate(self.rbt, self.jordan)
        sent = json.loads(urlopen.call_args.args[0].data)["messages"][0]["content"]
        for leaked in ("Alex", "Rivera", "alex@akevra.test", "555-123-4567", "1-01-11007", "4829301", "Jordan", "Northshore"):
            self.assertNotIn(leaked, sent)
        self.assertIn("[REDACTED]", sent)
        self.assertIn('"assessed_on": "2026-10"', sent)  # full dates reduced to year-month
        self.assertNotIn("2026-10-12", sent)
        self.assertEqual(created[0].prompt, sent)  # what was sent is exactly what is recorded
        event = created[0].events.get(action="generated")
        self.assertEqual(event.detail["redactions"], 5)  # full name, email, phone, cert, record number

    # --- REQ-031: complete explanation content ---------------------------------------------------
    def test_every_suggestion_carries_evidence_assumptions_limitations_and_scoped_confidence(self):
        for s in self.generate():
            content = s["content"]
            for key in ("evidence", "assumptions", "limitations", "alternatives", "teaching_question", "confidence"):
                self.assertIn(key, content)
            self.assertTrue(content["evidence"] and content["limitations"] and content["teaching_question"])
            for item in content["evidence"]:
                self.assertIn(item["category"], ai.EVIDENCE_CATEGORIES)
                self.assertIn(item["strength"], ai.EVIDENCE_STRENGTHS)
            self.assertEqual(content["confidence"], ai.confidence_from(content["evidence"]))
        self.assertEqual(ai.confidence_from([{"strength": "strong"}, {"strength": "limited"}]), "low")
        self.assertEqual(ai.confidence_from([{"strength": "strong"}]), "high")

        self.enable_ai()
        with mock.patch.object(ai, "_api_key", return_value="test-key"), \
                mock.patch("urllib.request.urlopen", return_value=self.mocked_reply(self.TWO)):
            created = ai.generate(self.rbt, self.jordan)
        self.assertEqual(created[0].content["confidence"], "high")
        self.assertEqual((created[0].content["assumptions"], created[0].content["teaching_question"]), (["a"], "q?"))
        self.assertEqual(created[1].content["evidence"][1], {"text": "y", "category": "other", "strength": "limited"})
        self.assertEqual(created[1].content["confidence"], "low")
        self.assertTrue(created[1].content["teaching_question"])  # a default question when the model gave none

    # --- REQ-032: teaching mode by default, justification only when selected ---------------------
    def test_teaching_mode_is_the_default_and_must_be_revealed_before_accepting(self):
        jordan = self.as_user("jordan@akevra.test")
        suggestions = self.generate(jordan, mode=None)  # no mode sent
        self.assertEqual({s["presentation_mode"] for s in suggestions}, {"teaching"})
        first, second = suggestions[0], suggestions[1]
        self.assertIsNone(first["revealed_at"])
        blocked = self.dispose(jordan, first["id"], action="accept")
        self.assertEqual(blocked.status_code, 400)
        self.assertIn("Reveal", str(blocked.data))

        alex = self.as_user("alex@akevra.test")
        self.assertEqual(alex.post(f"/api/v1/ai-suggestions/{first['id']}/reveal", {}, format="json").status_code, 404)
        revealed = jordan.post(f"/api/v1/ai-suggestions/{first['id']}/reveal", {"reflection": "Docs are the gap."}, format="json")
        self.assertEqual(revealed.status_code, 200, revealed.data)
        self.assertTrue(revealed.data["revealed_at"])
        self.assertEqual(revealed.data["supervisor_reflection"], "Docs are the gap.")
        self.assertEqual([e["action"] for e in revealed.data["events"]], ["generated", "revealed"])
        self.assertTrue(AuditEvent.objects.filter(action="ai_suggestion.reveal", entity_id=first["id"]).exists())
        self.assertEqual(jordan.post(f"/api/v1/ai-suggestions/{first['id']}/reveal", {}, format="json").status_code, 400)
        self.assertEqual(self.dispose(jordan, first["id"], action="accept").status_code, 200)
        # Rejecting doesn't need a reveal
        self.assertEqual(self.dispose(jordan, second["id"], action="reject", note="Not now").status_code, 200)

        direct = self.generate(jordan, mode="justification")
        self.assertEqual({s["presentation_mode"] for s in direct}, {"justification"})
        self.assertEqual(jordan.post(f"/api/v1/ai-suggestions/{direct[0]['id']}/reveal", {}, format="json").status_code, 400)
        self.assertEqual(self.dispose(jordan, direct[0]["id"], action="accept").status_code, 200)
        bad = jordan.post(f"/api/v1/relationships/{self.rbt.id}/ai-suggestions/generate", {"mode": "auto"}, format="json")
        self.assertEqual(bad.status_code, 400)

    # --- M7 clinical quality: compliance, admin and missing information are never supervisee goals ---------------
    def test_suggestion_types_keep_compliance_admin_and_gaps_out_of_the_plan(self):
        jordan = self.as_user("jordan@akevra.test")
        with mock.patch.object(ai, "_compliance_shortfalls", return_value=[{"month": "2026-09", "unmet_requirements": ["Supervision percentage"]}]):
            suggestions = self.generate(jordan)
        by_rule = {s["content"]["title"]: s for s in suggestions}
        goal = by_rule["Raise Session documentation from Not Yet Demonstrated to Proficient"]
        self.assertEqual((goal["type"], goal["is_goal"]), ("competency_goal", True))
        compliance = next(s for s in suggestions if s["type"] == "compliance_responsibility")
        self.assertFalse(compliance["is_goal"])
        self.assertIn("Supervisor action", compliance["content"]["title"])
        admin = next(s for s in suggestions if s["type"] == "administrative_task")
        before = self.plan_state()
        refused = self.dispose(jordan, compliance["id"], action="accept")
        self.assertEqual(refused.status_code, 400)
        self.assertIn("not a supervisee development goal", str(refused.data))
        noted = self.dispose(jordan, compliance["id"], action="acknowledge", note="Scheduling fixed")
        self.assertEqual(noted.status_code, 200, noted.data)
        self.assertEqual(noted.data["disposition"], "acknowledged")
        self.assertEqual(self.plan_state(), before)  # nothing entered the plan
        self.assertEqual(self.dispose(jordan, goal["id"], action="acknowledge").status_code, 400)
        self.assertEqual(self.dispose(jordan, admin["id"], action="acknowledge").status_code, 200)

    def test_classification_guard_overrides_the_model(self):
        item = ai.classify({"type": "competency_goal", "evidence": [{"text": "Missed hours", "category": "compliance_record", "strength": "strong"}]})
        self.assertEqual(item["type"], "information_gap")  # no documented deficit
        item = ai.classify({"type": "development_opportunity", "evidence": [{"text": "Missed hours", "category": "compliance_record", "strength": "strong"}]})
        self.assertEqual(item["type"], "compliance_responsibility")  # compliance records only
        item = ai.classify({"type": "competency_goal", "evidence": [{"text": "Rated Emerging", "category": "official_assessment", "strength": "strong"}]})
        self.assertEqual(item["type"], "competency_goal")
