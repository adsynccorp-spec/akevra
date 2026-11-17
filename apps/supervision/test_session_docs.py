"""Guided, AI-assisted session documentation: the six acceptance criteria."""

import io
import json
from unittest import mock

from django.core.exceptions import ValidationError
from django.core.management import call_command
from django.test import TestCase
from rest_framework.test import APIClient

from apps.accounts.models import UserAccount
from apps.audit.models import AuditEvent
from apps.core.models import HardDeleteNotAllowed
from apps.core.tests import PASSWORD
from apps.supervision import ai, session_docs
from apps.supervision.models import SessionDocDraft, SessionDocumentation, SupervisoryRelationship

ANSWERS = {
    "observed": "Alex ran DTT with accurate prompting; data sheet filled late.",
    "feedback_given": "Modelled faster data recording between trials.",
    "changes_assigned": "Record data within the trial for the next 3 sessions.",
    "next_focus": "Data timing and error correction.",
}


class SessionDocumentationTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        call_command("seed_sprint0", verbosity=0)
        cls.jordan = UserAccount.objects.get(email="jordan@akevra.test")
        cls.alex = UserAccount.objects.get(email="alex@akevra.test")
        cls.rbt = SupervisoryRelationship.objects.get(supervisor=cls.jordan, supervisee=cls.alex)

    def setUp(self):
        patcher = mock.patch.object(ai, "_api_key", return_value="")
        patcher.start()
        self.addCleanup(patcher.stop)
        self.jordan_c = self.as_user("jordan@akevra.test")
        body = {"occurred_on": "2026-10-06", "start_time": "10:00", "end_time": "11:00", "session_type": "individual",
                "meeting_method": "in_person", "topics": "DTT data collection review",
                "activities": "Observed DTT and modelled error correction"}
        created = self.jordan_c.post(f"/api/v1/relationships/{self.rbt.id}/sessions", body, format="json")
        self.assertEqual(created.status_code, 201, created.data)
        self.sid = created.data["id"]
        self.base = f"/api/v1/sessions/{self.sid}/documentation"

    def as_user(self, email):
        client = APIClient()
        token = client.post("/api/v1/auth/login", {"email": email, "password": PASSWORD}, format="json").data["token"]
        client.credentials(HTTP_AUTHORIZATION=f"Bearer {token}")
        return client

    def drafted(self):
        self.assertEqual(self.jordan_c.put(self.base, ANSWERS, format="json").status_code, 200)
        response = self.jordan_c.post(f"{self.base}/generate", {}, format="json")
        self.assertEqual(response.status_code, 200, response.data)
        return response.data

    # 1. Both outputs drafted from one workflow, linked to the correct session and relationship
    def test_one_workflow_drafts_both_outputs_for_the_session(self):
        empty = self.jordan_c.get(self.base).data
        self.assertFalse(empty["exists"])
        self.assertEqual([q["key"] for q in empty["questions"]], list(session_docs.ANSWER_KEYS))
        self.assertEqual(self.jordan_c.post(f"{self.base}/generate", {}, format="json").status_code, 400)  # answers first
        data = self.drafted()
        self.assertEqual(set(data["outputs"]), {"supervisor_note", "supervisee_feedback"})
        note = data["outputs"]["supervisor_note"]["content"]
        self.assertEqual(set(note), {"observations", "activities_coaching", "performance_feedback",
                                     "procedures_adjustments", "follow_up_next_focus"})  # structured sections
        self.assertIn("Record data within the trial", note["follow_up_next_focus"])
        self.assertEqual(note["procedures_adjustments"], "Discussed: DTT data collection review")
        self.assertEqual(data["outputs"]["supervisee_feedback"]["content"]["follow_up"], ANSWERS["changes_assigned"])
        doc = SessionDocumentation.objects.get(session_id=self.sid)
        self.assertEqual(doc.session.cycle.relationship_id, self.rbt.id)
        # Saving again updates the same record: one documentation per session, never a duplicate
        self.jordan_c.put(self.base, {"next_focus": "Error correction"}, format="json")
        self.assertEqual(SessionDocumentation.objects.filter(session_id=self.sid).count(), 1)
        self.assertEqual(SessionDocumentation.objects.get(session_id=self.sid).observed, ANSWERS["observed"])

    # 2. Nothing official or visible to the supervisee without approval of THAT output
    def test_each_output_is_approved_separately_and_feedback_hidden_until_approved(self):
        self.drafted()
        alex = self.as_user("alex@akevra.test")
        self.assertEqual(alex.get(self.base).data["outputs"], {})
        self.assertEqual(alex.get("/api/v1/my-feedback").data["feedback"], [])
        approved_note = self.jordan_c.post(f"{self.base}/supervisor_note/approve", {}, format="json").data
        self.assertEqual(approved_note["outputs"]["supervisor_note"]["status"], "approved")
        self.assertEqual(approved_note["outputs"]["supervisee_feedback"]["status"], "draft")
        self.assertEqual(alex.get(self.base).data["outputs"], {})  # approving the note shares nothing
        approved = self.jordan_c.post(f"{self.base}/supervisee_feedback/approve", {}, format="json")
        self.assertEqual(approved.status_code, 200)
        shared = alex.get(self.base).data
        self.assertEqual(list(shared["outputs"]), ["supervisee_feedback"])
        self.assertTrue(shared["can_acknowledge"])
        self.assertEqual(len(alex.get("/api/v1/my-feedback").data["feedback"]), 1)
        # Approved outputs are locked: no edit, no regeneration, no second approval
        self.assertEqual(self.jordan_c.patch(f"{self.base}/supervisee_feedback", {"content": {"strengths": "x"}}, format="json").status_code, 400)
        self.assertEqual(self.jordan_c.post(f"{self.base}/generate", {"kinds": ["supervisor_note"]}, format="json").status_code, 400)
        self.assertEqual(self.jordan_c.post(f"{self.base}/supervisor_note/approve", {}, format="json").status_code, 400)

    # 3. Feedback only for the assigned supervisee; the private note is never shared
    def test_private_note_never_shared_and_feedback_only_for_the_assigned_supervisee(self):
        self.drafted()
        self.jordan_c.post(f"{self.base}/supervisor_note/approve", {}, format="json")
        self.jordan_c.post(f"{self.base}/supervisee_feedback/approve", {}, format="json")
        alex_view = json.dumps(self.as_user("alex@akevra.test").get(self.base).data)
        self.assertNotIn("supervisor_note", alex_view)
        self.assertNotIn('"observations"', alex_view)  # the note's text
        self.assertNotIn('"answers"', alex_view)  # the raw answers aren't shared, only the approved summary
        self.assertNotIn('"prompt"', alex_view)
        # An authorized audit reviewer (Clinical Director) inspects everything read-only
        director_c = self.as_user("director@akevra.test")
        director = director_c.get(self.base).data
        self.assertTrue(director["can_review"])
        self.assertFalse(director["can_document"])
        self.assertIn("observations", director["outputs"]["supervisor_note"]["content"])
        self.assertEqual(director_c.put(self.base, {"observed": "x"}, format="json").status_code, 403)
        self.assertEqual(self.as_user("summit.supervisor@akevra.test").get(self.base).status_code, 404)  # other org
        self.assertEqual(self.as_user("morgan@akevra.test").get("/api/v1/my-feedback").data["feedback"], [])
        # Only the Supervisor writes
        alex = self.as_user("alex@akevra.test")
        self.assertEqual(alex.put(self.base, {"observed": "x"}, format="json").status_code, 403)
        self.assertEqual(alex.post(f"{self.base}/supervisee_feedback/approve", {}, format="json").status_code, 403)
        # The audit log's readable fields never carry the note text
        for event in AuditEvent.objects.filter(action__startswith="session_documentation."):
            self.assertNotIn("data sheet", json.dumps(event.metadata))

    # 4. Draft ID, prompt / model version, edits and approvals recorded
    def test_provenance_edits_and_approvals_are_recorded(self):
        self.drafted()
        edited = self.jordan_c.patch(f"{self.base}/supervisee_feedback",
                                     {"content": {"strengths": "Accurate prompting during DTT."}}, format="json").data
        feedback = edited["outputs"]["supervisee_feedback"]
        self.assertTrue(feedback["edited"])
        self.assertEqual(feedback["content"]["coaching"], ANSWERS["feedback_given"])  # other parts kept
        regenerated = self.jordan_c.post(f"{self.base}/generate", {"kinds": ["supervisor_note"]}, format="json").data
        self.assertEqual(len(regenerated["drafts"]), 3)
        self.assertTrue(regenerated["outputs"]["supervisee_feedback"]["edited"])  # regenerating one leaves the other
        self.jordan_c.post(f"{self.base}/supervisee_feedback/approve", {}, format="json")
        data = self.jordan_c.get(self.base).data
        draft = data["drafts"][0]
        self.assertTrue(draft["identifier"] and draft["created_at"] and draft["prompt"])
        self.assertEqual(draft["model_name"], session_docs.TEMPLATE_GENERATOR)
        self.assertEqual([e["action"] for e in data["events"]],
                         ["answers_saved", "generated", "generated", "edited", "regenerated", "approved"])
        edit_event = data["events"][3]
        self.assertEqual(edit_event["detail"]["after"]["strengths"], "Accurate prompting during DTT.")
        self.assertEqual(data["events"][-1]["actor"]["name"], self.jordan.full_name)
        row = SessionDocDraft.objects.get(identifier=draft["identifier"])
        row.content = {"text": "changed"}
        with self.assertRaises(ValidationError):
            row.save()
        with self.assertRaises(HardDeleteNotAllowed):
            row.delete()

    # 5. Supervisee acknowledgment recorded separately from approval
    def test_acknowledgment_is_separate_from_approval(self):
        self.drafted()
        alex = self.as_user("alex@akevra.test")
        self.assertEqual(alex.post(f"{self.base}/acknowledge", {}, format="json").status_code, 400)  # nothing approved
        self.jordan_c.post(f"{self.base}/supervisee_feedback/approve", {}, format="json")
        self.assertEqual(self.jordan_c.post(f"{self.base}/acknowledge", {}, format="json").status_code, 403)
        acknowledged = alex.post(f"{self.base}/acknowledge", {}, format="json").data
        feedback = acknowledged["outputs"]["supervisee_feedback"]
        self.assertEqual(feedback["acknowledged_by"]["name"], self.alex.full_name)
        self.assertEqual(feedback["approved_by"]["name"], self.jordan.full_name)
        self.assertNotEqual(feedback["acknowledged_at"], feedback["approved_at"])
        self.assertFalse(acknowledged["can_acknowledge"])
        self.assertEqual(alex.post(f"{self.base}/acknowledge", {}, format="json").status_code, 400)
        self.assertEqual(alex.get("/api/v1/my-feedback").data["unacknowledged"], 0)

    # 6 / safeguards: AI only when the organization opted in, and only redacted text is sent
    def test_ai_drafts_use_redacted_context_only_when_enabled(self):
        org = self.rbt.organization
        reply = {"model": "claude-sonnet-5-5", "stop_reason": "end_turn", "content": [{"type": "text", "text": json.dumps({
            "supervisor_note": {"observations": "Obs", "activities_coaching": "BST", "performance_feedback": "PF",
                                "procedures_adjustments": "Discussed only", "follow_up_next_focus": "Next"},
            "supervisee_feedback": {"strengths": "S", "improvements": "I", "coaching": "C", "follow_up": "F"}})}]}
        response = mock.MagicMock()
        response.__enter__.return_value = io.BytesIO(json.dumps(reply).encode())
        self.jordan_c.put(self.base, {**ANSWERS, "observed": "Alex Rivera (555-123-4567) ran DTT."}, format="json")
        with mock.patch.object(ai, "_api_key", return_value="k"), mock.patch("urllib.request.urlopen") as urlopen:
            self.jordan_c.post(f"{self.base}/generate", {}, format="json")
        urlopen.assert_not_called()  # AI is off for the organization

        org.ai_enabled = True
        org.save(update_fields=["ai_enabled", "updated_at"])
        with mock.patch.object(ai, "_api_key", return_value="k"), \
                mock.patch("urllib.request.urlopen", return_value=response) as urlopen:
            data = self.jordan_c.post(f"{self.base}/generate", {}, format="json").data
        sent = json.loads(urlopen.call_args.args[0].data)["messages"][0]["content"]
        for leaked in ("Alex", "Rivera", "555-123-4567", "2026-10-06"):
            self.assertNotIn(leaked, sent)
        self.assertIn("[REDACTED]", sent)
        self.assertEqual(data["outputs"]["supervisee_feedback"]["content"]["coaching"], "C")
        latest = data["drafts"][-1]
        self.assertEqual((latest["model_name"], latest["prompt"]), ("claude-sonnet-5-5", sent))
        self.assertEqual(latest["redactions"], 2)

    # Single entry: the guided answers start from the session record
    def test_guided_answers_are_prefilled_from_the_session_record(self):
        data = self.jordan_c.get(self.base).data
        self.assertEqual(data["prefill"]["observed"], "Observed DTT and modelled error correction")
        self.assertNotIn("prefill", self.as_user("alex@akevra.test").get(self.base).data)

    # Audit: reviewers see the original draft and every revision
    def test_reviewers_can_inspect_original_and_revised_text(self):
        self.drafted()
        self.jordan_c.patch(f"{self.base}/supervisor_note", {"content": {"observations": "Revised observation."}}, format="json")
        review = self.as_user("director@akevra.test").get(self.base).data
        original = review["drafts"][0]["content"]["observations"]
        edit = next(e for e in review["events"] if e["action"] == "edited")
        self.assertEqual((edit["detail"]["before"]["observations"], edit["detail"]["after"]["observations"]),
                         (original, "Revised observation."))
