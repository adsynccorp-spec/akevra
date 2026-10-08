"""Supervision percentage and cumulative-hours calculations.

The two hours models never share a calculation path:

- Supervised Fieldwork: only the trainee's self-logged, Supervisor-verified HoursEntry rows.
- RBT / BCaBA ongoing supervision: supervision time only from Supervisor-authored
  SupervisionSession records, measured against the Supervisee's self-attested monthly
  service hours (ServiceHoursAttestation).

The evaluate_* functions are pure (numbers in, result out) so an approved test-case set can
run against them directly. Rule values come from the ComplianceRuleSet in force for the month;
without one, the BACB-based defaults below are used and the result says they are unapproved.
"""

from datetime import date
from decimal import Decimal

from django.db.models import Count, Q, Sum

from apps.supervision.models import (
    ComplianceRuleSet,
    FieldworkSubtype,
    HoursKind,
    HoursStatus,
    ServiceHoursAttestation,
    SessionType,
    SupervisionFormat,
    SupervisionSession,
    SupervisionTrack,
)

ZERO = Decimal("0")
HUNDRED = Decimal("100")

# BACB-based defaults. Every value is a ComplianceRuleParameter key, so an approved rule set
# overrides any of them without a code change.
DEFAULT_RULES = {
    SupervisionTrack.RBT_ONGOING: {
        "min_supervision_percent": "5",
        "min_contacts": "2",
        "min_individual_contacts": "1",
        "min_observation_contacts": "1",
    },
    SupervisionTrack.SUPERVISED_FIELDWORK: {
        "supervised.min_supervision_percent": "5",
        "supervised.min_contacts": "4",
        "supervised.total_required_hours": "2000",
        "concentrated.min_supervision_percent": "10",
        "concentrated.min_contacts": "6",
        "concentrated.total_required_hours": "1500",
        "min_observation_contacts": "1",
        "max_group_percent": "50",
        "min_monthly_hours": "20",
        "max_monthly_hours": "130",
        "noncompliant_month_counts": "false",
    },
}

DEFAULTS_NOTE = "BACB-based default parameters, pending AKEVRA approval."


class Rules:
    """Rule parameters for one track and month, with where they came from."""

    def __init__(self, values, source):
        self.values = values
        self.source = source

    def number(self, key):
        return Decimal(str(self.values[key]))

    def flag(self, key):
        return str(self.values.get(key, "false")).strip().lower() in {"1", "true", "yes"}


def default_rules(track):
    values = DEFAULT_RULES.get(track)
    if values is None:
        return None
    return Rules(dict(values), {"name": "Default (BACB-based)", "approved": False, "note": DEFAULTS_NOTE})


def load_rules(track, on_date):
    """The rule set in force for this track on this date, layered over the defaults."""
    rule_set = (
        ComplianceRuleSet.objects.filter(
            supervision_track=track,
            record_status="active",
            effective_from__lte=on_date,
        )
        .filter(Q(effective_to__isnull=True) | Q(effective_to__gte=on_date))
        .order_by("-effective_from")
        .prefetch_related("parameters")
        .first()
    )
    if rule_set is None:
        return default_rules(track)
    values = dict(DEFAULT_RULES.get(track, {}))
    values.update({p.key: p.value for p in rule_set.parameters.all() if p.record_status == "active"})
    return Rules(
        values,
        {
            "name": rule_set.name,
            "approved": True,
            "effective_from": rule_set.effective_from.isoformat(),
            "rule_set_id": str(rule_set.id),
        },
    )


def _dec(value):
    return ZERO if value is None else Decimal(str(value))


def _out(value):
    """Exact Decimal for comparisons; rounded number for the API."""
    return None if value is None else float(value.quantize(Decimal("0.01")))


def _check(key, label, required, actual, met):
    return {"key": key, "label": label, "required": required, "actual": actual, "met": bool(met)}


# ---------------------------------------------------------------------------
# Supervised Fieldwork (self-logged, Supervisor-verified hours only)
# ---------------------------------------------------------------------------

def evaluate_fieldwork_month(rules, fieldwork_type, *, total_hours, supervision_hours, group_hours,
                             contacts, observation_contacts):
    """One month of verified fieldwork hours. `total_hours` includes the supervision hours."""
    total = _dec(total_hours)
    supervision = _dec(supervision_hours)
    group = _dec(group_hours)
    prefix = f"{fieldwork_type}."
    required_percent = rules.number(prefix + "min_supervision_percent")
    min_contacts = int(rules.number(prefix + "min_contacts"))
    min_observations = int(rules.number("min_observation_contacts"))
    max_group_percent = rules.number("max_group_percent")
    min_hours = rules.number("min_monthly_hours")
    max_hours = rules.number("max_monthly_hours")

    percent = supervision / total * HUNDRED if total > 0 else None
    group_percent = group / supervision * HUNDRED if supervision > 0 else ZERO
    required_supervision = total * required_percent / HUNDRED

    checks = [
        _check("min_monthly_hours", "Minimum fieldwork hours", _out(min_hours), _out(total), total >= min_hours),
        _check("max_monthly_hours", "Maximum fieldwork hours", _out(max_hours), _out(total), total <= max_hours),
        _check(
            "supervision_percent", "Supervision percentage", _out(required_percent), _out(percent),
            percent is not None and percent >= required_percent,
        ),
        _check("contacts", "Supervision contacts", min_contacts, contacts, contacts >= min_contacts),
        _check(
            "observation_contacts", "Contacts with client observation", min_observations,
            observation_contacts, observation_contacts >= min_observations,
        ),
        _check(
            "group_percent", "Group supervision (maximum share)", _out(max_group_percent),
            _out(group_percent), group_percent <= max_group_percent,
        ),
    ]
    met = all(check["met"] for check in checks)
    countable = total if (met or rules.flag("noncompliant_month_counts")) else ZERO
    return {
        "model": "fieldwork_verified_hours",
        "fieldwork_type": fieldwork_type,
        "status": "met" if met else "shortfall",
        "met": met,
        "total_hours": _out(total),
        "supervision_hours": _out(supervision),
        "independent_hours": _out(total - supervision),
        "supervision_percent": _out(percent),
        "required_supervision_percent": _out(required_percent),
        "required_supervision_hours": _out(required_supervision),
        "remaining_supervision_hours": _out(max(ZERO, required_supervision - supervision)),
        "countable_hours": _out(countable),
        "checks": checks,
        "_countable": countable,
    }


def evaluate_fieldwork_cumulative(rules, months):
    """`months` is a list of (fieldwork_type, countable_hours). Supervised and concentrated
    hours count toward their own totals; progress is the sum of the two fractions, which is
    how a mixed supervised/concentrated record converts to one completion figure."""
    supervised = sum((_dec(h) for t, h in months if t == FieldworkSubtype.SUPERVISED), ZERO)
    concentrated = sum((_dec(h) for t, h in months if t == FieldworkSubtype.CONCENTRATED), ZERO)
    need_supervised = rules.number("supervised.total_required_hours")
    need_concentrated = rules.number("concentrated.total_required_hours")
    fraction = supervised / need_supervised + concentrated / need_concentrated
    left = max(ZERO, 1 - fraction)
    return {
        "supervised_hours": _out(supervised),
        "concentrated_hours": _out(concentrated),
        "required_supervised_hours": _out(need_supervised),
        "required_concentrated_hours": _out(need_concentrated),
        "percent_complete": _out(min(HUNDRED, fraction * HUNDRED)),
        "remaining_if_supervised": _out(left * need_supervised),
        "remaining_if_concentrated": _out(left * need_concentrated),
        "complete": fraction >= 1,
    }


# ---------------------------------------------------------------------------
# RBT / BCaBA ongoing supervision (session-derived supervision, attested service hours)
# ---------------------------------------------------------------------------

def evaluate_ongoing_month(rules, *, service_hours, supervision_minutes, contacts,
                           individual_contacts, observation_contacts):
    """One month: supervision time from Supervisor-authored sessions over the Supervisee's
    self-attested service hours. `service_hours` is None until the Supervisee attests."""
    supervision = _dec(supervision_minutes) / Decimal("60")
    required_percent = rules.number("min_supervision_percent")
    base = {
        "model": "session_derived_supervision",
        "service_hours": _out(None if service_hours is None else _dec(service_hours)),
        "supervision_hours": _out(supervision),
        "required_supervision_percent": _out(required_percent),
        "contacts": contacts,
    }
    if service_hours is None:
        return {**base, "status": "awaiting_service_hours", "met": False, "supervision_percent": None,
                "required_supervision_hours": None, "remaining_supervision_hours": None, "checks": []}

    service = _dec(service_hours)
    if service == 0:
        # No services delivered this month, so there is nothing to supervise against
        return {**base, "status": "no_service_hours", "met": True, "supervision_percent": None,
                "required_supervision_hours": 0.0, "remaining_supervision_hours": 0.0, "checks": []}

    percent = supervision / service * HUNDRED
    required_supervision = service * required_percent / HUNDRED
    min_contacts = int(rules.number("min_contacts"))
    min_individual = int(rules.number("min_individual_contacts"))
    min_observations = int(rules.number("min_observation_contacts"))
    checks = [
        _check("supervision_percent", "Supervision percentage", _out(required_percent), _out(percent),
               percent >= required_percent),
        _check("contacts", "Supervision contacts", min_contacts, contacts, contacts >= min_contacts),
        _check("individual_contacts", "Individual contacts", min_individual, individual_contacts,
               individual_contacts >= min_individual),
        _check("observation_contacts", "Contacts with client observation", min_observations,
               observation_contacts, observation_contacts >= min_observations),
    ]
    met = all(check["met"] for check in checks)
    return {
        **base,
        "status": "met" if met else "shortfall",
        "met": met,
        "supervision_percent": _out(percent),
        "required_supervision_hours": _out(required_supervision),
        "remaining_supervision_hours": _out(max(ZERO, required_supervision - supervision)),
        "checks": checks,
    }


# ---------------------------------------------------------------------------
# Database side: gather one cycle's numbers for the matching model, then evaluate
# ---------------------------------------------------------------------------

def _rules_pending(track):
    return {
        "status": "rules_pending",
        "met": None,
        "detail": "No compliance rule set is configured for this pathway yet.",
        "track": track,
    }


def _fieldwork_month_for(cycle, rules):
    verified = cycle.hours_entries.filter(status=HoursStatus.VERIFIED, record_status="active")
    supervision_rows = verified.filter(kind=HoursKind.SUPERVISION)
    totals = verified.aggregate(total_hours=Sum("hours"))
    supervision = supervision_rows.aggregate(
        supervision_hours=Sum("hours"),
        group_hours=Sum("hours", filter=Q(supervision_format=SupervisionFormat.GROUP)),
        contact_count=Count("id"),
        observation_count=Count("id", filter=Q(client_observation=True)),
    )
    return evaluate_fieldwork_month(
        rules,
        cycle.fieldwork_type or FieldworkSubtype.SUPERVISED,
        total_hours=totals["total_hours"],
        supervision_hours=supervision["supervision_hours"],
        group_hours=supervision["group_hours"],
        contacts=supervision["contact_count"],
        observation_contacts=supervision["observation_count"],
    )


def _ongoing_month_for(cycle, rules):
    sessions = cycle.sessions.filter(record_status="active").aggregate(
        minutes=Sum("duration_minutes"),
        contact_count=Count("id"),
        individual_count=Count("id", filter=Q(session_type=SessionType.INDIVIDUAL)),
        observation_count=Count("id", filter=Q(client_observation=True)),
    )
    attestation = cycle.service_attestations.filter(record_status="active").first()
    return evaluate_ongoing_month(
        rules,
        service_hours=None if attestation is None else attestation.hours,
        supervision_minutes=sessions["minutes"],
        contacts=sessions["contact_count"],
        individual_contacts=sessions["individual_count"],
        observation_contacts=sessions["observation_count"],
    )


def cycle_compliance(cycle):
    track = cycle.relationship.supervision_track
    rules = load_rules(track, date(cycle.year, cycle.month, 1))
    if rules is None:
        return _rules_pending(track)
    if track == SupervisionTrack.SUPERVISED_FIELDWORK:
        result = _fieldwork_month_for(cycle, rules)
        result.pop("_countable")
    elif track in (SupervisionTrack.RBT_ONGOING, SupervisionTrack.BCABA_ONGOING):
        result = _ongoing_month_for(cycle, rules)
    else:
        return {"status": "not_compliance_bearing", "met": None, "track": track}
    return {**result, "track": track, "rules": rules.source}


def relationship_compliance(relationship, cycles):
    """Every cycle's result in order plus the running totals. `cycles` are this relationship's
    cycles, oldest first."""
    track = relationship.supervision_track
    months = []
    if track == SupervisionTrack.SUPERVISED_FIELDWORK:
        countable = []
        last_rules = None
        for cycle in cycles:
            rules = load_rules(track, date(cycle.year, cycle.month, 1))
            last_rules = rules
            result = _fieldwork_month_for(cycle, rules)
            countable.append((result["fieldwork_type"], result.pop("_countable")))
            months.append({"cycle_id": str(cycle.id), "year": cycle.year, "month": cycle.month,
                           **result, "rules": rules.source})
        rules = last_rules or default_rules(track)
        return {"track": track, "months": months, "cumulative": evaluate_fieldwork_cumulative(rules, countable)}

    if track in (SupervisionTrack.RBT_ONGOING, SupervisionTrack.BCABA_ONGOING):
        months_met = 0
        for cycle in cycles:
            result = cycle_compliance(cycle)
            months.append({"cycle_id": str(cycle.id), "year": cycle.year, "month": cycle.month, **result})
            months_met += 1 if result.get("met") else 0
        # Totals from the raw rows, not the rounded monthly figures
        cycle_ids = [cycle.id for cycle in cycles]
        minutes = SupervisionSession.objects.filter(cycle_id__in=cycle_ids, record_status="active").aggregate(
            total=Sum("duration_minutes")
        )["total"]
        service_total = _dec(
            ServiceHoursAttestation.objects.filter(cycle_id__in=cycle_ids, record_status="active").aggregate(
                total=Sum("hours")
            )["total"]
        )
        supervision_total = _dec(minutes) / Decimal("60")
        return {
            "track": track,
            "months": months,
            "cumulative": {
                "service_hours": _out(service_total),
                "supervision_hours": _out(supervision_total),
                "supervision_percent": _out(supervision_total / service_total * HUNDRED) if service_total else None,
                "months_met": months_met,
                "months_total": len(months),
            },
        }

    return {"track": track, "months": [], "cumulative": None, "status": "not_compliance_bearing"}
