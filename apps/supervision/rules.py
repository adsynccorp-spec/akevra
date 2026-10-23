"""Reviewing and approving the compliance rule set for an organization (Settings → Compliance Rules).

An Administrator reviews each pathway's BACB default parameters, may adjust values, and approves
them from a date. The approval is stored as the organization's own ComplianceRuleSet (with who,
when and a note), which then takes precedence over the defaults; results stop showing
"pending AKEVRA approval". A new approval supersedes the previous one, which stays on file.
"""

from datetime import timedelta
from decimal import Decimal, InvalidOperation

from django.core.exceptions import ValidationError
from django.db import transaction
from django.utils import timezone

from apps.supervision.compliance import DEFAULT_RULES, load_rules
from apps.supervision.models import ComplianceRuleParameter, ComplianceRuleSet, SupervisionTrack

PARAM_LABELS = {
    "min_supervision_percent": "Minimum supervision (% of service hours)",
    "min_contacts": "Minimum supervision contacts per month",
    "min_individual_contacts": "Minimum individual contacts per month",
    "min_observation_contacts": "Minimum contacts with client observation",
    "reduced_supervision_percent": "Reduced supervision % (after the hour threshold)",
    "reduced_after_service_hours": "Post-certification hours before the reduced %",
    "biweekly_min_minutes": "Minimum minutes in each half of the month (first 1,000 hours)",
    "max_group_percent": "Maximum group share of supervised time (%)",
    "quarterly_observations": "Client observations per quarter",
    "supervised.min_supervision_percent": "Supervised fieldwork: minimum supervision %",
    "supervised.min_contacts": "Supervised fieldwork: minimum contacts per month",
    "supervised.total_required_hours": "Supervised fieldwork: total hours required",
    "concentrated.min_supervision_percent": "Concentrated fieldwork: minimum supervision %",
    "concentrated.min_contacts": "Concentrated fieldwork: minimum contacts per month",
    "concentrated.total_required_hours": "Concentrated fieldwork: total hours required",
    "concentrated_multiplier": "Concentrated hours multiplier (mixed fieldwork)",
    "min_monthly_hours": "Minimum fieldwork hours per month",
    "max_monthly_hours": "Maximum fieldwork hours per month",
    "min_unrestricted_percent": "Minimum unrestricted activities (% of all fieldwork)",
}
TRACKS = [SupervisionTrack.RBT_ONGOING, SupervisionTrack.BCABA_ONGOING, SupervisionTrack.SUPERVISED_FIELDWORK]


def _version_key(start):
    return "2027" if start.year >= 2027 else "current"


def _serialize_set(rule_set):
    return {
        "id": str(rule_set.id),
        "name": rule_set.name,
        "effective_from": rule_set.effective_from.isoformat(),
        "effective_to": rule_set.effective_to.isoformat() if rule_set.effective_to else None,
        "status": rule_set.record_status,
        "approved_by": rule_set.approved_by.full_name if rule_set.approved_by_id else None,
        "approved_at": rule_set.approved_at.isoformat() if rule_set.approved_at else None,
        "approval_note": rule_set.approval_note,
        "parameters": {p.key: p.value for p in rule_set.parameters.all() if p.record_status == "active"},
    }


def catalog(organization):
    today = timezone.localdate()
    tracks = []
    for track in TRACKS:
        versions = [
            {
                "key": _version_key(start),
                "effective_from": start.isoformat() if start.year > 2000 else None,
                "label": "BACB 2027 requirements" if start.year >= 2027 else "BACB current requirements",
                "parameters": [{"key": k, "label": PARAM_LABELS.get(k, k), "default": str(v)} for k, v in sorted(values.items())],
            }
            for start, values in DEFAULT_RULES.get(track, [])
        ]
        in_force = load_rules(track, today, organization.id)
        history = ComplianceRuleSet.objects.filter(supervision_track=track, organization=organization) \
            .exclude(record_status="archived").select_related("approved_by").prefetch_related("parameters").order_by("-created_at")
        tracks.append({
            "track": track,
            "label": SupervisionTrack(track).label,
            "versions": versions,
            "in_force": in_force.source,
            "approved": bool(in_force.source.get("approved")),
            "approvals": [_serialize_set(r) for r in history],
        })
    return tracks


def approve(organization, actor, *, track, effective_from, overrides=None, note=""):
    """`overrides` is {version key ("current" / "2027"): {parameter: value}}; anything not given keeps the default."""
    if track not in TRACKS:
        raise ValidationError("Unknown pathway.")
    by_version = {}
    for version, values in (overrides or {}).items():
        if not isinstance(values, dict):
            raise ValidationError("Overrides must be grouped by version.")
        by_version[str(version)] = {str(k): str(v).strip() for k, v in values.items() if str(v).strip() != ""}
    for version, values in by_version.items():
        defaults = next((v for start, v in DEFAULT_RULES[track] if _version_key(start) == version), None)
        if defaults is None:
            raise ValidationError(f"Unknown version '{version}'.")
        unknown = sorted(set(values) - set(defaults))
        if unknown:
            raise ValidationError(f"Unknown parameter(s): {', '.join(unknown)}.")
        for key, value in values.items():
            try:
                if Decimal(value) < 0:
                    raise InvalidOperation
            except InvalidOperation:
                raise ValidationError(f"{PARAM_LABELS.get(key, key)} must be a non-negative number.") from None
    now = timezone.now()
    created = []
    with transaction.atomic():
        # Earlier approvals stay on file, superseded
        for old in ComplianceRuleSet.objects.filter(supervision_track=track, organization=organization, record_status="active"):
            old.record_status = "superseded"
            old.save(update_fields=["record_status", "updated_at"])
        versions = DEFAULT_RULES[track]
        for index, (version_start, values) in enumerate(versions):
            nxt = versions[index + 1][0] if index + 1 < len(versions) else None
            start = max(version_start, effective_from)
            end = nxt - timedelta(days=1) if nxt else None
            if end and end < start:
                continue
            label = "2027 requirements" if version_start.year >= 2027 else "current requirements"
            rule_set = ComplianceRuleSet.objects.create(
                name=f"{organization.name} — approved ({label})", effective_from=start, effective_to=end,
                supervision_track=track, organization=organization, approved_by=actor, approved_at=now, approval_note=note or "",
            )
            for key, value in {**values, **by_version.get(_version_key(version_start), {})}.items():
                ComplianceRuleParameter.objects.create(rule_set=rule_set, key=key, value=str(value))
            created.append(rule_set)
    return created
