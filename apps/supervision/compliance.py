"""Supervision percentage and cumulative-hours calculations.

The two hours models never share a calculation path:

- Supervised Fieldwork: only the trainee's self-logged, Supervisor-verified HoursEntry rows.
- RBT / BCaBA ongoing supervision: supervision time only from Supervisor-authored
  SupervisionSession records, measured against the Supervisee's self-attested monthly
  service hours (ServiceHoursAttestation).

The evaluate_* functions are pure (numbers in, result out) so an approved test-case set can
run against them directly. Rule values come from the ComplianceRuleSet in force for the month;
without one, the BACB defaults below are used and the result says they are unapproved.

Sources for the defaults (BACB handbooks, updated 06/2026):
- BCBA Handbook: fieldwork requirements, "Adjusting and Documenting Fieldwork Hours When
  Monthly Requirements Are Not Met", combining fieldwork types (x1.33), 2027 requirements.
- RBT Handbook: ongoing supervision (5%, two contacts, one individual, one observation).
- BCaBA Handbook: ongoing supervision (5% / 2% after 1,000 hours, monthly meeting, group no
  more than individual, observation each quarter) and the 2027 ongoing requirements.
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

# Every value is a ComplianceRuleParameter key, so an approved rule set overrides any of them
# without a code change. Each track lists its defaults by the month they take effect.
_FIELDWORK_2022 = {
    "supervised.min_supervision_percent": "5",
    "supervised.min_contacts": "4",
    "supervised.total_required_hours": "2000",
    "concentrated.min_supervision_percent": "10",
    "concentrated.min_contacts": "6",
    "concentrated.total_required_hours": "1500",
    "concentrated_multiplier": "1.33",
    "min_observation_contacts": "1",
    "max_group_percent": "50",
    "min_monthly_hours": "20",
    "max_monthly_hours": "130",
}
_BCABA_CURRENT = {
    "min_supervision_percent": "5",
    "reduced_supervision_percent": "2",
    "reduced_after_service_hours": "1000",
    "biweekly_min_minutes": "60",
    "min_contacts": "1",
    "max_group_percent": "50",
    "quarterly_observations": "1",
}
DEFAULT_RULES = {
    SupervisionTrack.RBT_ONGOING: [
        (date(2000, 1, 1), {
            "min_supervision_percent": "5",
            "min_contacts": "2",
            "min_individual_contacts": "1",
            "min_observation_contacts": "1",
        }),
    ],
    SupervisionTrack.SUPERVISED_FIELDWORK: [
        (date(2000, 1, 1), _FIELDWORK_2022),
        # 2027: concentrated 7.5%, up to 160 hours a month, no contact count
        (date(2027, 1, 1), {
            **_FIELDWORK_2022,
            "concentrated.min_supervision_percent": "7.5",
            "supervised.min_contacts": "0",
            "concentrated.min_contacts": "0",
            "max_monthly_hours": "160",
        }),
    ],
    SupervisionTrack.BCABA_ONGOING: [
        (date(2000, 1, 1), _BCABA_CURRENT),
        # 2027: a flat 5%, at least one meeting a month, no limit on group supervision
        (date(2027, 1, 1), {
            "min_supervision_percent": "5",
            "min_contacts": "1",
            "quarterly_observations": "1",
        }),
    ],
}

DEFAULTS_NOTE = "BACB default parameters (handbooks updated 06/2026), pending AKEVRA approval."


class Rules:
    """Rule parameters for one track and month, with where they came from."""

    def __init__(self, values, source):
        self.values = values
        self.source = source

    def has(self, key):
        return key in self.values

    def number(self, key):
        return Decimal(str(self.values[key]))


def _defaults_for(track, on_date):
    versions = [values for start, values in DEFAULT_RULES.get(track, []) if start <= on_date]
    return dict(versions[-1]) if versions else None


def default_rules(track, on_date=None):
    values = _defaults_for(track, on_date or date.today())
    if values is None:
        return None
    return Rules(values, {"name": "Default (BACB)", "approved": False, "note": DEFAULTS_NOTE})


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
        return default_rules(track, on_date)
    values = _defaults_for(track, on_date) or {}
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
    """One month of verified fieldwork hours. `total_hours` includes the supervision hours.

    When a requirement is missed, the month's eligible hours follow the BACB adjustment table:
    no observation or under the minimum -> none; over the maximum -> independent hours removed
    down to the maximum; group over its share -> group hours reduced; percentage short ->
    independent hours removed until it is met; contacts short -> prorated by contacts held.
    Concentrated hours may not be adjusted, so a concentrated month that misses anything has none."""
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
        _check(
            "observation_contacts", "Contacts with client observation", min_observations,
            observation_contacts, observation_contacts >= min_observations,
        ),
        _check(
            "group_percent", "Group supervision (maximum share)", _out(max_group_percent),
            _out(group_percent), group_percent <= max_group_percent,
        ),
    ]
    if min_contacts:
        checks.insert(3, _check("contacts", "Supervision contacts", min_contacts, contacts, contacts >= min_contacts))
    met = all(check["met"] for check in checks)

    eligible, adjustments = total, []
    if not met:
        eligible, adjustments = _adjust_fieldwork_month(
            fieldwork_type, total=total, supervision=supervision, group=group, contacts=contacts,
            observation_contacts=observation_contacts, required_percent=required_percent,
            min_contacts=min_contacts, min_observations=min_observations,
            max_group_percent=max_group_percent, min_hours=min_hours, max_hours=max_hours,
        )
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
        "eligible_hours": _out(eligible),
        "adjustments": adjustments,
        "checks": checks,
        "_eligible": eligible,
    }


def _adjust_fieldwork_month(fieldwork_type, *, total, supervision, group, contacts, observation_contacts,
                            required_percent, min_contacts, min_observations, max_group_percent,
                            min_hours, max_hours):
    """Eligible hours and the adjustments that produced them (BACB adjustment table, in order)."""
    if observation_contacts < min_observations:
        return ZERO, [{"rule": "observation", "detail": "No observation with a client: no hours are eligible for the month."}]
    if total < min_hours:
        return ZERO, [{"rule": "min_monthly_hours", "detail": f"Fewer than {_out(min_hours):g} hours: no hours are eligible for the month."}]
    if fieldwork_type == FieldworkSubtype.CONCENTRATED:
        return ZERO, [{"rule": "concentrated", "detail": "Concentrated hours may not be prorated or adjusted, so none are eligible this month."}]

    adjustments = []
    independent = total - supervision
    individual = supervision - group

    allowed_group = individual * max_group_percent / (HUNDRED - max_group_percent) if max_group_percent < HUNDRED else group
    if group > allowed_group:
        removed = group - allowed_group
        group, supervision, total = allowed_group, supervision - removed, total - removed
        adjustments.append({"rule": "group_percent", "removed_hours": _out(removed),
                            "detail": "Group supervision reduced until it no longer exceeds individual supervision."})

    if total > max_hours:
        removed = min(independent, total - max_hours)
        independent, total = independent - removed, total - removed
        adjustments.append({"rule": "max_monthly_hours", "removed_hours": _out(removed),
                            "detail": f"Independent hours removed until the total is {_out(max_hours):g}."})

    if total > 0 and supervision / total * HUNDRED < required_percent:
        allowed_total = supervision * HUNDRED / required_percent
        removed = min(independent, total - allowed_total)
        independent, total = independent - removed, total - removed
        adjustments.append({"rule": "supervision_percent", "removed_hours": _out(removed),
                            "detail": "Independent hours removed until the supervision percentage is met."})

    if min_contacts and contacts < min_contacts:
        share = Decimal(contacts) / Decimal(min_contacts)
        removed = total - total * share
        total = total * share
        adjustments.append({"rule": "contacts", "removed_hours": _out(removed),
                            "detail": f"Hours prorated to {contacts} of {min_contacts} required contacts."})
    return total, adjustments


def evaluate_fieldwork_cumulative(rules, months):
    """`months` is a list of (fieldwork_type, eligible_hours). With only one type, its own total
    applies (2,000 supervised or 1,500 concentrated). Mixed: concentrated hours x 1.33 plus
    supervised hours must reach the supervised total."""
    supervised = sum((_dec(h) for t, h in months if t == FieldworkSubtype.SUPERVISED), ZERO)
    concentrated = sum((_dec(h) for t, h in months if t == FieldworkSubtype.CONCENTRATED), ZERO)
    need_supervised = rules.number("supervised.total_required_hours")
    need_concentrated = rules.number("concentrated.total_required_hours")
    multiplier = rules.number("concentrated_multiplier")

    if supervised == 0:
        fraction = concentrated / need_concentrated
        remaining_concentrated = max(ZERO, need_concentrated - concentrated)
        remaining_supervised = max(ZERO, need_supervised - concentrated * multiplier)
    else:
        combined = supervised + concentrated * multiplier
        fraction = combined / need_supervised
        remaining_supervised = max(ZERO, need_supervised - combined)
        remaining_concentrated = remaining_supervised / multiplier
    return {
        "supervised_hours": _out(supervised),
        "concentrated_hours": _out(concentrated),
        "combined_hours": _out(supervised + concentrated * multiplier),
        "concentrated_multiplier": _out(multiplier),
        "required_supervised_hours": _out(need_supervised),
        "required_concentrated_hours": _out(need_concentrated),
        "percent_complete": _out(min(HUNDRED, fraction * HUNDRED)),
        "remaining_if_supervised": _out(remaining_supervised),
        "remaining_if_concentrated": _out(remaining_concentrated),
        "complete": fraction >= 1,
    }


# ---------------------------------------------------------------------------
# RBT / BCaBA ongoing supervision (session-derived supervision, attested service hours)
# ---------------------------------------------------------------------------

def evaluate_ongoing_month(rules, *, service_hours, supervision_minutes, contacts, individual_contacts,
                           observation_contacts, group_minutes=0, first_half_minutes=None,
                           second_half_minutes=None, prior_service_hours=0, quarter_observations=None,
                           quarter_end=False):
    """One month: supervision time from Supervisor-authored sessions over the Supervisee's
    self-attested service hours. `service_hours` is None until the Supervisee attests.
    Only the checks whose parameters the rules define are applied, so RBT and BCaBA share it."""
    supervision = _dec(supervision_minutes) / Decimal("60")
    required_percent = rules.number("min_supervision_percent")
    if rules.has("reduced_after_service_hours") and _dec(prior_service_hours) >= rules.number("reduced_after_service_hours"):
        required_percent = rules.number("reduced_supervision_percent")
    reduced_tier = required_percent < rules.number("min_supervision_percent")

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
        # No services delivered this month, so the month's supervision requirements don't apply
        return {**base, "status": "no_service_hours", "met": True, "supervision_percent": None,
                "required_supervision_hours": 0.0, "remaining_supervision_hours": 0.0, "checks": []}

    percent = supervision / service * HUNDRED
    required_supervision = service * required_percent / HUNDRED
    checks = [
        _check("supervision_percent", "Supervision percentage", _out(required_percent), _out(percent),
               percent >= required_percent),
    ]
    if rules.has("min_contacts"):
        needed = int(rules.number("min_contacts"))
        checks.append(_check("contacts", "Supervision contacts", needed, contacts, contacts >= needed))
    if rules.has("min_individual_contacts"):
        needed = int(rules.number("min_individual_contacts"))
        checks.append(_check("individual_contacts", "Individual contacts", needed, individual_contacts,
                             individual_contacts >= needed))
    if rules.has("min_observation_contacts"):
        needed = int(rules.number("min_observation_contacts"))
        checks.append(_check("observation_contacts", "Contacts with client observation", needed,
                             observation_contacts, observation_contacts >= needed))
    if rules.has("max_group_percent"):
        limit = rules.number("max_group_percent")
        share = _dec(group_minutes) / _dec(supervision_minutes) * HUNDRED if supervision_minutes else ZERO
        checks.append(_check("group_percent", "Group supervision (maximum share)", _out(limit), _out(share),
                             share <= limit))
    if rules.has("biweekly_min_minutes") and not reduced_tier and first_half_minutes is not None:
        needed = int(rules.number("biweekly_min_minutes"))
        least = min(first_half_minutes, second_half_minutes or 0)
        checks.append(_check("biweekly_minutes", "Supervision minutes in each half of the month", needed,
                             least, least >= needed))
    if rules.has("quarterly_observations") and quarter_observations is not None:
        needed = int(rules.number("quarterly_observations"))
        # Only due by the quarter's last month; earlier months are not failed for it
        checks.append(_check("quarterly_observations", "Client observation this quarter", needed,
                             quarter_observations, quarter_observations >= needed or not quarter_end))
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
    active = cycle.sessions.filter(record_status="active")
    sessions = active.aggregate(
        minutes=Sum("duration_minutes"),
        group_minutes=Sum("duration_minutes", filter=Q(session_type=SessionType.GROUP)),
        first_half=Sum("duration_minutes", filter=Q(occurred_on__day__lte=14)),
        second_half=Sum("duration_minutes", filter=Q(occurred_on__day__gte=15)),
        contact_count=Count("id"),
        individual_count=Count("id", filter=Q(session_type=SessionType.INDIVIDUAL)),
        observation_count=Count("id", filter=Q(client_observation=True)),
    )
    attestation = cycle.service_attestations.filter(record_status="active").first()
    relationship_id = cycle.relationship_id
    earlier = Q(cycle__year__lt=cycle.year) | Q(cycle__year=cycle.year, cycle__month__lt=cycle.month)
    prior_service = ServiceHoursAttestation.objects.filter(
        earlier, cycle__relationship_id=relationship_id, record_status="active"
    ).aggregate(total=Sum("hours"))["total"]
    quarter_start = (cycle.month - 1) // 3 * 3 + 1
    quarter_observations = SupervisionSession.objects.filter(
        cycle__relationship_id=relationship_id, cycle__year=cycle.year,
        cycle__month__gte=quarter_start, cycle__month__lte=cycle.month,
        client_observation=True, record_status="active",
    ).count()
    return evaluate_ongoing_month(
        rules,
        service_hours=None if attestation is None else attestation.hours,
        supervision_minutes=sessions["minutes"] or 0,
        contacts=sessions["contact_count"],
        individual_contacts=sessions["individual_count"],
        observation_contacts=sessions["observation_count"],
        group_minutes=sessions["group_minutes"] or 0,
        first_half_minutes=sessions["first_half"] or 0,
        second_half_minutes=sessions["second_half"] or 0,
        prior_service_hours=prior_service or 0,
        quarter_observations=quarter_observations,
        quarter_end=cycle.month % 3 == 0,
    )


def cycle_compliance(cycle):
    track = cycle.relationship.supervision_track
    rules = load_rules(track, date(cycle.year, cycle.month, 1))
    if rules is None:
        return _rules_pending(track)
    if track == SupervisionTrack.SUPERVISED_FIELDWORK:
        result = _fieldwork_month_for(cycle, rules)
        result.pop("_eligible")
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
        eligible = []
        last_rules = None
        for cycle in cycles:
            rules = load_rules(track, date(cycle.year, cycle.month, 1))
            last_rules = rules
            result = _fieldwork_month_for(cycle, rules)
            eligible.append((result["fieldwork_type"], result.pop("_eligible")))
            months.append({"cycle_id": str(cycle.id), "year": cycle.year, "month": cycle.month,
                           **result, "rules": rules.source})
        rules = last_rules or default_rules(track)
        return {"track": track, "months": months, "cumulative": evaluate_fieldwork_cumulative(rules, eligible)}

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
