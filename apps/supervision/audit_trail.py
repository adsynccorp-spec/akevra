"""A4 audit trail: who did what, and when, for any record a user can see.

Two sources feed the trail:
  * row-level rows written by the `write_row_audit` DB trigger (`create` / `update`, with the
    full before/after row and, since core 0015, the acting user), and
  * application events written through `apps.audit.services.write_audit`
    (e.g. "hours.verify", "appointment.schedule"), which carry the actor and intent.

One user action usually produces both (the app event and the row change it caused). They are
merged into one entry: the app event names the action, the row diff shows what changed.
Older trigger rows that pre-date actor capture have the actor inferred from the row itself
(e.g. `verified_by_id` on a verification) and are flagged `actor_inferred`.

Authorization: a record resolves to its relationship (or, for supervisee-owned records such as
service hours, to the supervisee); viewing needs `audit.view` there. Field values are shown
only to those who may also see the clinical record (`clinical.view`); an Administrator sees
who changed which fields and when, not the clinical content.
"""

import json
from datetime import date, timedelta
from uuid import UUID

from django.db.models import Q

from apps.accounts.models import UserAccount
from apps.audit.models import AuditEvent
from apps.rbac.services import has_permission
from apps.supervision import models as m

# ---------------------------------------------------------------------------
# Record types
# ---------------------------------------------------------------------------

# canonical entity_type (the table name) -> (model, label, how to reach the relationship)
#   "self"         the relationship itself
#   "relationship" a direct FK
#   "cycle"        through the monthly cycle
#   "plan"         through the development plan
#   "supervisee"   owned by the supervisee, not a relationship
RECORD_TYPES = {
    "supervisory_relationship": (m.SupervisoryRelationship, "Relationship", "self"),
    "supervisee_intake": (m.SuperviseeIntake, "Intake", "relationship"),
    "development_plan": (m.DevelopmentPlan, "Development plan", "relationship"),
    "development_plan_goal": (m.DevelopmentPlanGoal, "Goal", "plan"),
    "development_plan_competency": (m.DevelopmentPlanCompetency, "Competency", "plan"),
    "development_plan_milestone": (m.DevelopmentPlanMilestone, "Milestone", "plan"),
    "monthly_cycle": (m.MonthlyCycle, "Monthly cycle", "relationship"),
    "assignment": (m.Assignment, "Assignment", "cycle"),
    "hours_entry": (m.HoursEntry, "Hours entry", "cycle"),
    "supervision_session": (m.SupervisionSession, "Session", "cycle"),
    "appointment": (m.Appointment, "Appointment", "relationship"),
    "competency_assessment": (m.CompetencyAssessment, "Competency assessment", "relationship"),
    "review_comment": (m.ReviewComment, "Review comment", "relationship"),
    "ai_suggestion": (m.AISuggestion, "AI suggestion", "relationship"),
    "relationship_conclusion": (m.RelationshipConclusion, "Conclusion", "relationship"),
    "service_hours_record": (m.ServiceHoursRecord, "Service hours", "supervisee"),
    "external_supervision_record": (m.ExternalSupervisionRecord, "External supervision", "supervisee"),
}

ALIASES = {
    "relationship": "supervisory_relationship",
    "intake": "supervisee_intake",
    "plan": "development_plan",
    "idp": "development_plan",
    "goal": "development_plan_goal",
    "competency": "development_plan_competency",
    "milestone": "development_plan_milestone",
    "cycle": "monthly_cycle",
    "hours": "hours_entry",
    "session": "supervision_session",
    "assessment": "competency_assessment",
    "comment": "review_comment",
    "conclusion": "relationship_conclusion",
    "service_hours": "service_hours_record",
    "external_supervision": "external_supervision_record",
}

# Child rows shown in a record's own trail: table -> (parent FK column in the row JSON, parent type)
CHILDREN = {
    "assignment_attachment": ("assignment_id", "assignment"),
    "session_attachment": ("session_id", "supervision_session"),
    "ai_suggestion_event": ("suggestion_id", "ai_suggestion"),
    "service_hours_review": ("record_id", "service_hours_record"),
}
CHILD_LABELS = {
    "assignment_attachment": "Attachment",
    "session_attachment": "Attachment",
    "ai_suggestion_event": "Disposition",
    "service_hours_review": "Review",
}


def normalize_entity_type(value):
    value = (value or "").strip().lower()
    value = ALIASES.get(value, value)
    return value if value in RECORD_TYPES else None


def type_label(entity_type):
    if entity_type in RECORD_TYPES:
        return RECORD_TYPES[entity_type][1]
    return CHILD_LABELS.get(entity_type, entity_type.replace("_", " ").capitalize())


def entity_type_choices():
    return [{"key": key, "label": label} for key, (_, label, _) in RECORD_TYPES.items()]


def resolve_record(entity_type, entity_id):
    """(record, relationship, supervisee) or None. RLS already limits rows to the workspace."""
    model, _, path = RECORD_TYPES[entity_type]
    try:
        UUID(str(entity_id))
    except ValueError:
        return None
    related = {
        "self": ["supervisor", "supervisee"],
        "relationship": ["relationship__supervisor", "relationship__supervisee"],
        "cycle": ["cycle__relationship__supervisor", "cycle__relationship__supervisee"],
        "plan": ["plan__relationship__supervisor", "plan__relationship__supervisee"],
        "supervisee": ["supervisee"],
    }[path]
    obj = model.objects.select_related(*related).filter(pk=entity_id).first()
    if obj is None:
        return None
    if path == "self":
        return obj, obj, obj.supervisee
    if path == "relationship":
        return obj, obj.relationship, obj.relationship.supervisee
    if path == "cycle":
        return obj, obj.cycle.relationship, obj.cycle.relationship.supervisee
    if path == "plan":
        return obj, obj.plan.relationship, obj.plan.relationship.supervisee
    return obj, None, obj.supervisee


def supervisee_relationships(supervisee):
    return list(m.SupervisoryRelationship.objects.filter(supervisee=supervisee).select_related("supervisor", "supervisee"))


def can_view(user_account, relationship=None, supervisee=None, permission="audit.view"):
    """audit.view on the relationship; for a supervisee-owned record, organization-wide or on any
    relationship with that supervisee (i.e. as their Supervisor)."""
    if relationship is not None:
        return has_permission(user_account, permission, relationship=relationship)
    if has_permission(user_account, permission):
        return True
    return any(has_permission(user_account, permission, relationship=r) for r in supervisee_relationships(supervisee))


# ---------------------------------------------------------------------------
# Labels for records
# ---------------------------------------------------------------------------

def _d(value):
    return f"{value:%b %d, %Y}".replace(" 0", " ") if value else ""


def _month(year, month):
    return date(year, month, 1).strftime("%b %Y")


def record_label(entity_type, obj):
    if obj is None:
        return type_label(entity_type)
    if entity_type == "supervisory_relationship":
        return f"Relationship · {obj.supervisee.full_name}"
    if entity_type == "supervisee_intake":
        return "Intake"
    if entity_type == "development_plan":
        return f"Development plan v{obj.version_number}"
    if entity_type in ("development_plan_goal", "development_plan_milestone", "assignment"):
        return obj.title
    if entity_type == "development_plan_competency":
        return obj.name
    if entity_type == "monthly_cycle":
        return f"{_month(obj.year, obj.month)} cycle"
    if entity_type == "hours_entry":
        return f"Hours · {_d(obj.occurred_on)} ({float(obj.hours):g} h)"
    if entity_type == "supervision_session":
        return f"Session · {_d(obj.occurred_on)}"
    if entity_type == "appointment":
        return f"Appointment · {_d(obj.starts_at.date())}"
    if entity_type == "competency_assessment":
        return f"Assessment · {obj.competency_name or _d(obj.assessed_on)}"
    if entity_type == "review_comment":
        return "Review comment"
    if entity_type == "ai_suggestion":
        title = (obj.content or {}).get("title") if isinstance(obj.content, dict) else None
        return f"AI suggestion · {title}" if title else "AI suggestion"
    if entity_type == "relationship_conclusion":
        return "Relationship conclusion"
    if entity_type == "service_hours_record":
        source = f" · {obj.external_organization}" if obj.external_organization else ""
        return f"Service hours · {_month(obj.year, obj.month)}{source}"
    if entity_type == "external_supervision_record":
        return f"External supervision · {_d(obj.occurred_on)}"
    return type_label(entity_type)


def _record_labels(pairs):
    """{(entity_type, entity_id): label} for a set of (type, id) pairs, one query per type."""
    by_type = {}
    for entity_type, entity_id in pairs:
        by_type.setdefault(entity_type, set()).add(entity_id)
    labels = {}
    for entity_type, ids in by_type.items():
        if entity_type not in RECORD_TYPES:
            for i in ids:
                labels[(entity_type, i)] = type_label(entity_type)
            continue
        model = RECORD_TYPES[entity_type][0]
        valid = [i for i in ids if _is_uuid(i)]
        found = {str(o.pk): o for o in model.objects.filter(pk__in=valid)}
        for i in ids:
            labels[(entity_type, i)] = record_label(entity_type, found.get(i))
    return labels


def _is_uuid(value):
    try:
        UUID(str(value))
        return True
    except ValueError:
        return False


# ---------------------------------------------------------------------------
# Diffs
# ---------------------------------------------------------------------------

NOISE = {"id", "created_at", "updated_at", "organization_id", "archived_at", "plan_family_id"}
SECRET_MARKERS = ("password", "secret", "token", "hash", "otp", "mfa")
# FK columns that point at people: shown as names
PEOPLE = {"supervisor_id", "supervisee_id", "author_id", "actor_id", "recipient_id", "coordinator_id", "reviewer_id"}
# Structural links whose value means nothing to a reader
STRUCTURAL = {"cycle_id", "plan_id", "relationship_id", "appointment_id", "assignment_id", "session_id",
              "suggestion_id", "record_id", "event_id", "supersedes_id", "superseded_by_id", "batch_id",
              "related_goal_id", "related_competency_id", "plan_competency_id", "series_id", "target_entity_id"}
MAX_TEXT = 300


def _hidden(field):
    lowered = field.lower()
    return field in NOISE or any(marker in lowered for marker in SECRET_MARKERS)


def _is_person(field):
    return field in PEOPLE or field.endswith("_by_id")


def field_label(field):
    name = field[:-3] if field.endswith("_id") else field
    return name.replace("_", " ").capitalize()


def _display(value):
    if isinstance(value, (dict, list)):
        text = json.dumps(value, default=str)
        return "(structured data)" if len(text) > MAX_TEXT else text
    if isinstance(value, str) and len(value) > MAX_TEXT:
        return value[:MAX_TEXT] + "…"
    return value


def _empty(value):
    return value in (None, "", [], {})


def diff(before, after, *, people, show_values):
    """[{field, label, from, to}] for keys that changed (all non-empty keys on create)."""
    before = before or {}
    after = after or {}
    changes = []
    creating = not before
    for field in sorted(set(before) | set(after)):
        if _hidden(field):
            continue
        old, new = before.get(field), after.get(field)
        if creating:
            if _empty(new) or field in STRUCTURAL:
                continue
        elif old == new:
            continue
        change = {"field": field, "label": field_label(field)}
        if not show_values:
            change.update({"from": None, "to": None, "redacted": True})
        elif _is_person(field):
            change.update({"from": people.get(str(old), old) if old else None, "to": people.get(str(new), new) if new else None})
        else:
            change.update({"from": None if creating else _display(old), "to": _display(new)})
        changes.append(change)
    return changes


def _people_ids(events):
    ids = set()
    for e in events:
        if e.actor_user_account_id:
            ids.add(str(e.actor_user_account_id))
        for row in (e.before, e.after):
            if isinstance(row, dict):
                for key, value in row.items():
                    if value and _is_person(key) and _is_uuid(value):
                        ids.add(str(value))
    return ids


# ---------------------------------------------------------------------------
# Actions
# ---------------------------------------------------------------------------

APP_ACTIONS = {
    "hours.log": "Logged hours",
    "hours.verify": "Verified hours",
    "hours.return": "Returned hours for correction",
    "hours.correct": "Corrected hours",
    "hours.observation_minutes": "Recorded observation minutes",
    "assignment.issue": "Issued assignment",
    "assignment.draft": "Saved assignment draft",
    "assignment.draft_update": "Updated draft assignment",
    "assignment.submit": "Submitted assignment",
    "assignment.request_revision": "Requested a revision",
    "assignment.complete": "Marked assignment complete",
    "session.document": "Documented session",
    "session.draft": "Saved session draft",
    "session.draft_update": "Updated session draft",
    "cycle.status": "Changed month status",
    "milestone.transition": "Changed milestone status",
    "idp.revise": "Revised development plan",
    "appointment.schedule": "Scheduled appointment",
    "appointment.reschedule": "Rescheduled appointment",
    "appointment.confirm": "Confirmed appointment",
    "appointment.cancel": "Cancelled appointment",
    "appointment.complete": "Marked appointment held",
    "appointment.no_show": "Marked appointment no-show",
    "service_hours.record": "Recorded service hours",
    "service_hours.review": "Reviewed service hours",
    "service_hours.period": "Recorded service hours period",
    "external_supervision.record": "Recorded external supervision",
    "external_supervision.void": "Flagged external supervision as incorrect",
    "assessment.create": "Created assessment",
    "assessment.update": "Updated assessment",
    "assessment.submit": "Submitted assessment for review",
    "assessment.return": "Returned assessment",
    "assessment.finalize": "Made assessment official",
    "ai_suggestion.accept": "Accepted AI suggestion",
    "ai_suggestion.edit_accept": "Edited and accepted AI suggestion",
    "ai_suggestion.reject": "Rejected AI suggestion",
    "reminder.resolve": "Resolved reminder",
    "reminder.dismiss": "Dismissed reminder",
    "prep_note.edit": "Edited preparation note",
    "prior_hours.review": "Reviewed prior service hours",
    "review_comment.create": "Commented",
    "note.create": "Added note",
    "prep_note.save": "Saved preparation note",
}


def _humanize(action):
    return action.replace(".", " ").replace("_", " ").strip().capitalize()


def _status_label(value):
    return str(value).replace("_", " ").capitalize() if value else "—"


def describe(action, entity_type, before, after):
    """A short sentence for a row change with no app event."""
    noun = type_label(entity_type)
    if action == "create":
        return f"Created {noun.lower()}"
    before, after = before or {}, after or {}
    if before.get("record_status") != after.get("record_status") and after.get("record_status") not in (None, "active"):
        return f"{_status_label(after.get('record_status'))} {noun.lower()}"
    if "status" in after and before.get("status") != after.get("status"):
        return f"Status changed to {_status_label(after.get('status'))}"
    return f"Updated {noun.lower()}"


# Columns that name who made a row: on create, then on update when they change
CREATOR_FIELDS = ("documented_by_id", "logged_by_id", "issued_by_id", "entered_by_id", "author_id", "created_by_id",
                  "submitted_by_id", "assessed_by_id", "requested_by_id", "concluded_by_id", "uploaded_by_id",
                  "actor_id", "attested_by_id", "reviewer_id")
UPDATER_FIELDS = ("verified_by_id", "closed_by_id", "reviewed_by_id", "dispositioned_by_id", "voided_by_id",
                  "resolved_by_id", "determined_by_id", "fieldwork_standard_determined_by_id")


def _infer_actor(action, before, after):
    after, before = after or {}, before or {}
    if action == "create":
        for field in CREATOR_FIELDS:
            if after.get(field):
                return str(after[field])
        return None
    for field in UPDATER_FIELDS:
        if after.get(field) and after.get(field) != before.get(field):
            return str(after[field])
    return None


# ---------------------------------------------------------------------------
# Merge
# ---------------------------------------------------------------------------

PAIR_WINDOW = timedelta(seconds=60)   # a request's row changes precede its app event by at most this
SAME_TX = timedelta(seconds=2)        # unexplained row changes this close together, by one actor, are one action


def _is_row(event):
    return (event.metadata or {}).get("source") == "db_trigger"


def _order(event):
    # Rows before the app event they caused; rows stamped with one transaction time (before core
    # 0015) are ordered by Django's updated_at inside the row
    return (event.created_at, 0 if _is_row(event) else 1, event.action != "create",
            str((event.after or {}).get("updated_at") or ""))


def _compatible(row, actor_id):
    return not row.actor_user_account_id or not actor_id or row.actor_user_account_id == actor_id


def _group_rows(rows):
    """Consecutive row changes by one actor within SAME_TX become one entry."""
    groups = []
    for row in rows:
        last = groups[-1] if groups else None
        if (last and row.created_at - last[-1].created_at <= SAME_TX
                and _compatible(row, next((r.actor_user_account_id for r in last if r.actor_user_account_id), None))):
            last.append(row)
        else:
            groups.append([row])
    return groups


def app_timeline(organization, events):
    """{actor_id: [(created_at, app_event_id), ...]} of every app-level event by the actors of these
    row changes, around their time. A request's row changes are followed by its own app event before
    the actor's next request writes anything, so the next app event by the same actor marks which
    action a row change belongs to."""
    rows = [e for e in events if _is_row(e) and e.actor_user_account_id]
    if not rows:
        return {}
    actors = {r.actor_user_account_id for r in rows}
    low = min(r.created_at for r in rows) - SAME_TX
    high = max(r.created_at for r in rows) + PAIR_WINDOW
    timeline = {}
    qs = (AuditEvent.objects.filter(organization=organization, actor_user_account_id__in=actors,
                                    created_at__gte=low, created_at__lte=high)
          .exclude(metadata__contains={"source": "db_trigger"})
          .order_by("created_at").values_list("actor_user_account_id", "created_at", "id"))
    for actor, at, pk in qs:
        timeline.setdefault(actor, []).append((at, pk))
    return timeline


def _owner(row, timeline, apps_here):
    """The app event (among this record's) that caused this row change, or None."""
    if row.actor_user_account_id:
        for at, pk in timeline.get(row.actor_user_account_id, []):
            if at < row.created_at:
                continue
            if at - row.created_at > PAIR_WINDOW:
                return None
            return apps_here.get(pk)  # None when the next action was on another record
        return None
    # Legacy row without an actor: the app event on this record right after it
    for app in sorted(apps_here.values(), key=lambda a: a.created_at):
        if timedelta(0) <= app.created_at - row.created_at <= SAME_TX:
            return app
    return None


def merge(events, timeline=None):
    """Group raw AuditEvents into entries: [{"app": event|None, "rows": [trigger events]}].

    Row changes are attached to the app event that caused them; row changes no app event explains
    stand on their own (consecutive ones by one actor grouped)."""
    timeline = timeline or {}
    by_entity = {}
    for e in events:
        by_entity.setdefault((e.entity_type, e.entity_id), []).append(e)

    entries = []
    for evs in by_entity.values():
        evs.sort(key=_order)
        apps_here = {e.id: e for e in evs if not _is_row(e)}
        owned = {pk: [] for pk in apps_here}
        loose = []
        for row in (e for e in evs if _is_row(e)):
            app = _owner(row, timeline, apps_here)
            (owned[app.id] if app else loose).append(row)
        local = [{"app": app, "rows": owned[pk]} for pk, app in apps_here.items()]
        local.extend({"app": None, "rows": g} for g in _group_rows(loose))

        # Clock skew between the app server and the database can put a row just after its app
        # event: attach such a row to an app event that explains nothing yet
        for entry in [x for x in local if x["app"] is None]:
            first = entry["rows"][0]
            for host in local:
                if (host["app"] is not None and not host["rows"]
                        and abs(first.created_at - host["app"].created_at) <= SAME_TX
                        and _compatible(first, host["app"].actor_user_account_id)):
                    host["rows"] = entry["rows"]
                    entry["rows"] = []
                    break
        entries.extend(x for x in local if x["app"] is not None or x["rows"])
    return entries


def _entry_time(entry):
    return entry["app"].created_at if entry["app"] else entry["rows"][0].created_at


def serialize(entries, *, people, show_values, labels=None, me=None):
    out = []
    for entry in entries:
        app, rows = entry["app"], entry["rows"]
        first = rows[0] if rows else app
        entity_type, entity_id = first.entity_type, first.entity_id
        if rows:
            before = rows[0].before
            after = rows[-1].after
            row_action = "create" if rows[0].action == "create" else "update"
        else:
            before, after, row_action = app.before, app.after, None

        actor_id = (app.actor_user_account_id if app else None) or next((r.actor_user_account_id for r in rows if r.actor_user_account_id), None)
        inferred = False
        if actor_id is None and rows:
            actor_id = _infer_actor(row_action, rows[0].before, rows[-1].after)
            inferred = actor_id is not None
        actor_id = str(actor_id) if actor_id else None

        if app is not None and app.action in ("create", "update"):
            action = app.action
            summary = describe(app.action, entity_type, before, after)
        elif app is not None:
            action = app.action
            summary = APP_ACTIONS.get(app.action) or _humanize(app.action)
        else:
            action = row_action
            summary = describe(row_action, entity_type, before, after)

        changes = diff(before, after, people=people, show_values=show_values) if (before or after) else []
        reason = None
        if app is not None and show_values:
            meta = app.metadata or {}
            reason = meta.get("reason") or meta.get("note") or meta.get("rationale")
        out.append({
            "id": str((app or rows[0]).id),
            "at": _entry_time(entry).isoformat(),
            "action": action,
            "summary": summary,
            "entity_type": entity_type,
            "entity_type_label": type_label(entity_type),
            "entity_id": entity_id,
            "record_label": (labels or {}).get((entity_type, entity_id), type_label(entity_type)),
            "actor": {"id": actor_id, "name": people.get(actor_id) if actor_id else "System"} if actor_id else {"id": None, "name": "System"},
            "actor_inferred": inferred,
            "is_me": bool(me is not None and actor_id == str(me.id)),
            "source": "app+row" if app and rows else ("app" if app else "row"),
            "changes": changes,
            "reason": reason if isinstance(reason, str) else None,
        })
    return out


def _names(ids):
    valid = [i for i in ids if _is_uuid(i)]
    return {str(a.id): a.full_name for a in UserAccount.objects.filter(pk__in=valid)}


def _base(organization):
    return (AuditEvent.objects.filter(organization=organization)
            .exclude(action__endswith=".denied")
            .exclude(action="audit_trail.view"))


# ---------------------------------------------------------------------------
# Queries
# ---------------------------------------------------------------------------

def record_trail(organization, entity_type, entity_id, *, show_values, me=None):
    """Every event for one record (and its attachments / reviews), newest first."""
    entity_id = str(entity_id)
    q = Q(entity_type=entity_type, entity_id=entity_id)
    for child, (column, parent) in CHILDREN.items():
        if parent == entity_type:
            q |= Q(entity_type=child, **{f"after__{column}": entity_id})
    events = list(_base(organization).filter(q))
    entries = merge(events, app_timeline(organization, events))
    people = _names(_people_ids(events))
    labels = _record_labels({(e.entity_type, e.entity_id) for e in events} | {(entity_type, entity_id)})
    result = serialize(entries, people=people, show_values=show_values, labels=labels, me=me)
    result.sort(key=lambda e: e["at"], reverse=True)
    return result


def relationship_record_ids(relationship):
    """{entity_type: [ids]} for every record that belongs to the relationship."""
    rel = relationship
    ids = lambda qs: [str(pk) for pk in qs.values_list("pk", flat=True)]  # noqa: E731
    return {
        "supervisory_relationship": [str(rel.id)],
        "supervisee_intake": ids(m.SuperviseeIntake.objects.filter(relationship=rel)),
        "development_plan": ids(m.DevelopmentPlan.objects.filter(relationship=rel)),
        "development_plan_goal": ids(m.DevelopmentPlanGoal.objects.filter(plan__relationship=rel)),
        "development_plan_competency": ids(m.DevelopmentPlanCompetency.objects.filter(plan__relationship=rel)),
        "development_plan_milestone": ids(m.DevelopmentPlanMilestone.objects.filter(plan__relationship=rel)),
        "monthly_cycle": ids(m.MonthlyCycle.objects.filter(relationship=rel)),
        "assignment": ids(m.Assignment.objects.filter(cycle__relationship=rel)),
        "hours_entry": ids(m.HoursEntry.objects.filter(cycle__relationship=rel)),
        "supervision_session": ids(m.SupervisionSession.objects.filter(cycle__relationship=rel)),
        "appointment": ids(m.Appointment.objects.filter(relationship=rel)),
        "competency_assessment": ids(m.CompetencyAssessment.objects.filter(relationship=rel)),
        "review_comment": ids(m.ReviewComment.objects.filter(relationship=rel)),
        "ai_suggestion": ids(m.AISuggestion.objects.filter(relationship=rel)),
        "relationship_conclusion": ids(m.RelationshipConclusion.objects.filter(relationship=rel)),
        "service_hours_record": ids(m.ServiceHoursRecord.objects.filter(supervisee_id=rel.supervisee_id)),
        "external_supervision_record": ids(m.ExternalSupervisionRecord.objects.filter(supervisee_id=rel.supervisee_id)),
    }


def relationship_trail(relationship, *, show_values, entity_type=None, actor=None, me=None, page=1, page_size=50):
    record_ids = relationship_record_ids(relationship)
    if entity_type:
        record_ids = {entity_type: record_ids.get(entity_type, [])}
    q = Q()
    for kind, ids in record_ids.items():
        if ids:
            q |= Q(entity_type=kind, entity_id__in=ids)
    events = list(_base(relationship.organization).filter(q)) if q else []
    entries = merge(events, app_timeline(relationship.organization, events))
    people = _names(_people_ids(events))
    labels = _record_labels({(e.entity_type, e.entity_id) for e in events})
    result = serialize(entries, people=people, show_values=show_values, labels=labels, me=me)
    result.sort(key=lambda e: e["at"], reverse=True)

    actors = {}
    for e in result:
        if e["actor"]["id"]:
            actors[e["actor"]["id"]] = e["actor"]["name"]
    if actor:
        result = [e for e in result if e["actor"]["id"] == str(actor)]

    total = len(result)
    start = (page - 1) * page_size
    return {
        "count": total,
        "page": page,
        "page_size": page_size,
        "has_next": start + page_size < total,
        "results": result[start:start + page_size],
        "actors": [{"id": k, "name": v} for k, v in sorted(actors.items(), key=lambda kv: kv[1])],
        "entity_types": entity_type_choices(),
        "values_visible": show_values,
    }
