"""M7 A4 (audit trail: who did what and when, for every record type) and A5 (one consistent
review-comment trail across assignments, hours entries, monthly cycles, milestones and sessions)."""

from datetime import date

from django.core.management import call_command
from django.test import TestCase
from rest_framework.test import APIClient

from apps.accounts.models import UserAccount
from apps.audit.models import AuditEvent
from apps.core.tests import PASSWORD
from apps.supervision.models import (
    Assignment,
    DevelopmentPlanMilestone,
    HoursEntry,
    MonthlyCycle,
    Notification,
    ReviewComment,
    ServiceHoursRecord,
    SupervisionSession,
    SupervisionTrack,
    SupervisoryRelationship,
)
from apps.supervision.review import comments_for_relationship


class ReviewAuditTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        call_command("seed_sprint0", verbosity=0)
        cls.jordan = UserAccount.objects.get(email="jordan@akevra.test")
        cls.alex = UserAccount.objects.get(email="alex@akevra.test")
        cls.casey = UserAccount.objects.get(email="casey@akevra.test")
        cls.rbt = SupervisoryRelationship.objects.get(supervisor=cls.jordan, supervisee=cls.alex)
        cls.fieldwork = SupervisoryRelationship.objects.create(
            organization=cls.jordan.organization, supervisor=cls.jordan, supervisee=cls.casey,
            supervision_track=SupervisionTrack.SUPERVISED_FIELDWORK, fieldwork_subtype="supervised",
            started_on=date(2026, 6, 1), fieldwork_requirements="bacb_2022", fieldwork_standard_basis="application_year",
        )

    def as_user(self, email):
        client = APIClient()
        response = client.post("/api/v1/auth/login", {"email": email, "password": PASSWORD}, format="json")
        self.assertEqual(response.data.get("status"), "authenticated", response.data)
        client.credentials(HTTP_AUTHORIZATION=f"Bearer {response.data['token']}")
        return client

    # --- fixtures built through the API, so every row change carries its actor ----------------
    def make_records(self):
        jordan, casey = self.as_user("jordan@akevra.test"), self.as_user("casey@akevra.test")
        issued = jordan.post(f"/api/v1/relationships/{self.rbt.id}/assignments",
                             {"title": "Ethics scenario", "due_on": "2026-08-20"}, format="json")
        self.assertEqual(issued.status_code, 201, issued.data)
        session = jordan.post(f"/api/v1/relationships/{self.rbt.id}/sessions",
                              {"occurred_on": "2026-08-10", "duration_minutes": 60, "session_type": "individual", "meeting_method": "in_person", "topics": "Supervision review"}, format="json")
        self.assertEqual(session.status_code, 201, session.data)
        logged = casey.post(f"/api/v1/relationships/{self.fieldwork.id}/hours",
                            {"occurred_on": "2026-08-05", "hours": "6", "kind": "independent"}, format="json")
        self.assertEqual(logged.status_code, 201, logged.data)
        plan = jordan.post(f"/api/v1/relationships/{self.rbt.id}/development-plans", {
            "summary": "Initial IDP",
            "goals": [{"category": "data_collection", "title": "Independent data collection"}],
            "competencies": [{"name": "Session documentation", "target_level": "independent", "current_level": "developing"}],
            "milestones": [{"title": "Complete first documented supervision month", "status": "active"}],
        }, format="json")
        self.assertEqual(plan.status_code, 201, plan.data)
        return {
            "assignment": Assignment.objects.get(cycle__relationship=self.rbt, title="Ethics scenario"),
            "session": SupervisionSession.objects.get(cycle__relationship=self.rbt, occurred_on=date(2026, 8, 10)),
            "hours": HoursEntry.objects.get(cycle__relationship=self.fieldwork, occurred_on=date(2026, 8, 5)),
            "cycle": MonthlyCycle.objects.get(relationship=self.rbt, year=2026, month=8),
            "milestone": DevelopmentPlanMilestone.objects.get(id=plan.data["milestones"][0]["id"]),
        }

    def comment(self, client, target_type, target_id, body, **extra):
        return client.post("/api/v1/review-comments",
                           {"target_type": target_type, "target_id": str(target_id), "body": body, **extra}, format="json")

    # =====================================================================
    # A5: one feedback trail
    # =====================================================================
    def test_a5_comments_on_all_target_types_form_one_trail(self):
        r = self.make_records()
        jordan, alex, casey = self.as_user("jordan@akevra.test"), self.as_user("alex@akevra.test"), self.as_user("casey@akevra.test")

        posted = [
            self.comment(jordan, "assignment", r["assignment"].id, "Cite the code section in your answer."),
            self.comment(alex, "supervision_session", r["session"].id, "Thanks, the modelling helped."),
            self.comment(jordan, "monthly_cycle", r["cycle"].id, "August is on track."),
            self.comment(alex, "development_plan_milestone", r["milestone"].id, "I think I'm close on this one."),
        ]
        for response in posted:
            self.assertEqual(response.status_code, 201, response.data)
        hours = self.comment(jordan, "hours_entry", r["hours"].id, "Please add the client initials to the notes.")
        self.assertEqual(hours.status_code, 201, hours.data)
        self.assertEqual(self.comment(casey, "hours_entry", r["hours"].id, "Added.").status_code, 201)

        # Supervisor and Supervisee both appear in the RBT relationship's trail, oldest first,
        # across every target type with a readable label
        trail = alex.get(f"/api/v1/relationships/{self.rbt.id}/review-comments")
        self.assertEqual(trail.status_code, 200, trail.data)
        rows = trail.data["comments"]
        self.assertEqual([c["target_type"] for c in rows],
                         ["assignment", "supervision_session", "monthly_cycle", "development_plan_milestone"])
        self.assertEqual([c["author_role"] for c in rows], ["Supervisor", "Supervisee", "Supervisor", "Supervisee"])
        self.assertEqual(rows[0]["target_label"], "Ethics scenario")
        self.assertEqual(rows[1]["target_label"], "Session · Aug 10, 2026")
        self.assertEqual(rows[2]["target_label"], "Aug 2026 cycle")
        self.assertEqual(rows[3]["target_label"], "Complete first documented supervision month")
        self.assertEqual(rows, sorted(rows, key=lambda c: c["created_at"]))

        # Hours entries live on the fieldwork relationship's trail
        fieldwork = jordan.get(f"/api/v1/relationships/{self.fieldwork.id}/review-comments").data["comments"]
        self.assertEqual([(c["target_type"], c["author"]["name"]) for c in fieldwork],
                         [("hours_entry", self.jordan.full_name), ("hours_entry", self.casey.full_name)])
        self.assertTrue(fieldwork[0]["target_label"].startswith("Hours · Aug 5, 2026"))

        # Per-record listing
        on_session = jordan.get("/api/v1/review-comments", {"target_type": "session", "target_id": str(r["session"].id)})
        self.assertEqual(on_session.status_code, 200)
        self.assertEqual([c["body"] for c in on_session.data["comments"]], ["Thanks, the modelling helped."])
        self.assertTrue(on_session.data["can_comment"])

        # The other party is notified; the post is audited
        self.assertTrue(Notification.objects.filter(recipient=self.alex, kind="review_comment").exists())
        self.assertTrue(Notification.objects.filter(recipient=self.jordan, kind="review_comment").exists())
        self.assertTrue(AuditEvent.objects.filter(action="review_comment.create", actor_user_account=self.alex).exists())

        # Export helper
        exported = comments_for_relationship(self.rbt)
        self.assertEqual(len(exported), 4)
        self.assertEqual(set(exported[0]) >= {"target_type", "target_label", "body", "author", "author_role", "created_at"}, True)

    def test_a5_service_hours_record_needs_matching_relationship(self):
        alex = self.as_user("alex@akevra.test")
        self.assertEqual(alex.put(f"/api/v1/relationships/{self.rbt.id}/service-hours",
                                  {"year": 2026, "month": 8, "hours": "120"}, format="json").status_code, 200)
        record = ServiceHoursRecord.objects.get(supervisee=self.alex, year=2026, month=8, record_status="active")
        jordan = self.as_user("jordan@akevra.test")
        self.assertEqual(self.comment(jordan, "service_hours_record", record.id, "Matches the payroll export.").status_code, 400)
        wrong = self.comment(jordan, "service_hours_record", record.id, "x", relationship_id=str(self.fieldwork.id))
        self.assertEqual(wrong.status_code, 400)
        ok = self.comment(jordan, "service_hours_record", record.id, "Matches the payroll export.", relationship_id=str(self.rbt.id))
        self.assertEqual(ok.status_code, 201, ok.data)
        self.assertEqual(ok.data["target_label"], "Service hours · Aug 2026")

    def test_a5_comments_are_immutable_and_need_a_body(self):
        r = self.make_records()
        jordan = self.as_user("jordan@akevra.test")
        self.assertEqual(self.comment(jordan, "assignment", r["assignment"].id, "   ").status_code, 400)
        made = self.comment(jordan, "assignment", r["assignment"].id, "First thought.")
        self.assertEqual(made.status_code, 201)
        for method in (jordan.patch, jordan.put, jordan.delete):
            self.assertEqual(method("/api/v1/review-comments", {"body": "changed"}, format="json").status_code, 405)
            self.assertEqual(method(f"/api/v1/review-comments/{made.data['id']}", {"body": "changed"}, format="json").status_code, 404)
        self.assertEqual(ReviewComment.objects.get(pk=made.data["id"]).body, "First thought.")

    def test_a5_only_parties_comment_and_other_orgs_get_404(self):
        r = self.make_records()
        director = self.as_user("director@akevra.test")  # Clinical Director: may read, is not a party
        listed = director.get("/api/v1/review-comments", {"target_type": "assignment", "target_id": str(r["assignment"].id)})
        self.assertEqual(listed.status_code, 200)
        self.assertFalse(listed.data["can_comment"])
        self.assertEqual(self.comment(director, "assignment", r["assignment"].id, "Hello").status_code, 403)

        outsider = self.as_user("summit.supervisor@akevra.test")
        self.assertEqual(self.comment(outsider, "assignment", r["assignment"].id, "Hello").status_code, 404)
        self.assertEqual(outsider.get("/api/v1/review-comments", {"target_type": "assignment", "target_id": str(r["assignment"].id)}).status_code, 404)
        self.assertEqual(outsider.get(f"/api/v1/relationships/{self.rbt.id}/review-comments").status_code, 404)

        stranger = self.as_user("settings.admin@akevra.test")  # same org, not a party
        self.assertEqual(stranger.get(f"/api/v1/relationships/{self.rbt.id}/review-comments").status_code, 403)

    # =====================================================================
    # A4: audit trail
    # =====================================================================
    def trail(self, client, entity_type, entity_id):
        response = client.get("/api/v1/audit/trail", {"entity_type": entity_type, "entity_id": str(entity_id)})
        self.assertEqual(response.status_code, 200, response.data)
        return response.data

    def assert_who_what_when(self, events, actor, action_fragment):
        match = [e for e in events if action_fragment in (e["action"] + " " + e["summary"]).lower()]
        self.assertTrue(match, f"no {action_fragment!r} in {[(e['action'], e['summary']) for e in events]}")
        event = match[0]
        self.assertEqual(event["actor"]["name"], actor.full_name)
        self.assertTrue(event["at"])
        return event

    def test_a4_trail_shows_actor_action_and_time_for_each_record_type(self):
        r = self.make_records()
        jordan, alex = self.as_user("jordan@akevra.test"), self.as_user("alex@akevra.test")

        # Updates by different people
        self.assertEqual(alex.post(f"/api/v1/assignments/{r['assignment'].id}/submit", {"note": "Done"}, format="json").status_code, 200)
        self.assertEqual(jordan.post(f"/api/v1/hours/{r['hours'].id}/verify").status_code, 200)
        self.assertEqual(jordan.post(f"/api/v1/cycles/{r['cycle'].id}/status", {"status": "ready_for_review"}, format="json").status_code, 200)
        self.assertEqual(jordan.post(f"/api/v1/milestones/{r['milestone'].id}/transition",
                                     {"status": "achieved", "rationale": "Met criterion."}, format="json").status_code, 200)

        assignment = self.trail(jordan, "assignment", r["assignment"].id)
        self.assertEqual(assignment["record_label"], "Ethics scenario")
        self.assert_who_what_when(assignment["events"], self.jordan, "issue")
        submitted = self.assert_who_what_when(assignment["events"], self.alex, "submit")
        self.assertIn("status", [c["field"] for c in submitted["changes"]])
        self.assertNotIn("updated_at", [c["field"] for c in submitted["changes"]])
        # Newest first
        self.assertEqual(assignment["events"], sorted(assignment["events"], key=lambda e: e["at"], reverse=True))

        session = self.trail(jordan, "session", r["session"].id)
        self.assert_who_what_when(session["events"], self.jordan, "session")

        hours = self.trail(jordan, "hours_entry", r["hours"].id)
        self.assert_who_what_when(hours["events"], self.casey, "log")
        verified = self.assert_who_what_when(hours["events"], self.jordan, "verif")
        self.assertEqual(verified["action"], "hours.verify")
        self.assertEqual(verified["source"], "app+row")  # the app event and the row change it caused are one entry
        self.assertEqual(next(c for c in verified["changes"] if c["field"] == "verified_by_id")["to"], self.jordan.full_name)

        cycle = self.trail(jordan, "monthly_cycle", r["cycle"].id)
        changed = self.assert_who_what_when(cycle["events"], self.jordan, "month status")
        self.assertEqual(next(c for c in changed["changes"] if c["field"] == "status")["to"], "ready_for_review")

        milestone = self.trail(jordan, "milestone", r["milestone"].id)
        self.assert_who_what_when(milestone["events"], self.jordan, "create")
        moved = self.assert_who_what_when(milestone["events"], self.jordan, "milestone status")
        self.assertEqual(next(c for c in moved["changes"] if c["field"] == "status")["to"], "achieved")

        # A review comment is a record too
        made = self.comment(alex, "assignment", r["assignment"].id, "Submitted, please review.")
        comment_trail = self.trail(jordan, "review_comment", made.data["id"])
        self.assert_who_what_when(comment_trail["events"], self.alex, "comment")

    def test_a4_relationship_trail_is_paginated_and_filterable(self):
        r = self.make_records()
        jordan = self.as_user("jordan@akevra.test")
        whole = jordan.get(f"/api/v1/relationships/{self.rbt.id}/audit-trail", {"page_size": 3})
        self.assertEqual(whole.status_code, 200, whole.data)
        self.assertEqual(len(whole.data["results"]), 3)
        self.assertTrue(whole.data["has_next"])
        types = {e["entity_type"] for e in jordan.get(f"/api/v1/relationships/{self.rbt.id}/audit-trail", {"page_size": 200}).data["results"]}
        self.assertTrue({"assignment", "supervision_session", "monthly_cycle", "development_plan_milestone"} <= types)

        only = jordan.get(f"/api/v1/relationships/{self.rbt.id}/audit-trail", {"entity_type": "assignment"}).data
        self.assertTrue(only["results"])
        self.assertEqual({e["entity_type"] for e in only["results"]}, {"assignment"})
        self.assertEqual({e["entity_id"] for e in only["results"]}, {str(r["assignment"].id)})

        mine = jordan.get(f"/api/v1/relationships/{self.rbt.id}/audit-trail", {"actor": str(self.jordan.id), "page_size": 200}).data
        self.assertTrue(mine["results"])
        self.assertTrue(all(e["actor"]["id"] == str(self.jordan.id) for e in mine["results"]))
        self.assertIn(self.jordan.full_name, [a["name"] for a in mine["actors"]])

    def test_a4_supervisee_denied_other_org_cannot_see(self):
        r = self.make_records()
        alex = self.as_user("alex@akevra.test")
        denied = alex.get("/api/v1/audit/trail", {"entity_type": "assignment", "entity_id": str(r["assignment"].id)})
        self.assertEqual(denied.status_code, 403)
        self.assertEqual(alex.get(f"/api/v1/relationships/{self.rbt.id}/audit-trail").status_code, 403)

        outsider = self.as_user("summit.supervisor@akevra.test")
        for kind, record in r.items():
            response = outsider.get("/api/v1/audit/trail", {"entity_type": kind, "entity_id": str(record.id)})
            self.assertEqual(response.status_code, 404, kind)
        self.assertEqual(outsider.get(f"/api/v1/relationships/{self.rbt.id}/audit-trail").status_code, 404)

        # Organization-level oversight: Clinical Director sees values; an Administrator who is not
        # a party sees who changed which fields and when, not the clinical content
        director = self.as_user("director@akevra.test")
        self.assertTrue(self.trail(director, "session", r["session"].id)["values_visible"])
        admin_view = self.trail(self.as_user("settings.admin@akevra.test"), "session", r["session"].id)
        self.assertFalse(admin_view["values_visible"])
        self.assertTrue(admin_view["events"])
        self.assertTrue(all(change.get("redacted") for e in admin_view["events"] for change in e["changes"]))
