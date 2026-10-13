"""Record AKEVRA's approval of the compliance parameters.

Copies the default parameters (or the current values) into ComplianceRuleSet rows marked with the
approval name, so results stop showing "pending AKEVRA approval". Each default version keeps its own
effective window (e.g. the 2027 BACB changes from 2027-01-01). Edit a value afterwards in the Django
admin, or pass --set key=value.
"""

from datetime import date, timedelta

from django.core.management.base import BaseCommand, CommandError
from django.db import transaction

from apps.supervision.compliance import DEFAULT_RULES
from apps.supervision.models import ComplianceRuleParameter, ComplianceRuleSet


class Command(BaseCommand):
    help = "Record AKEVRA-approved compliance rule sets (from the BACB defaults, with optional overrides)."

    def add_arguments(self, parser):
        parser.add_argument("--name", default="AKEVRA Approved", help="Rule set name shown on the Compliance tab")
        parser.add_argument("--effective-from", default="2026-01-01", help="First month the approval applies to (YYYY-MM-DD)")
        parser.add_argument("--track", action="append", help="Only this pathway (repeatable); default all")
        parser.add_argument("--set", action="append", default=[], metavar="TRACK:KEY=VALUE",
                            help="Override one parameter, e.g. rbt_ongoing_supervision:min_supervision_percent=5")

    def handle(self, *args, **options):
        try:
            start = date.fromisoformat(options["effective_from"])
        except ValueError as exc:
            raise CommandError("--effective-from must be YYYY-MM-DD") from exc
        overrides = {}
        for item in options["set"]:
            try:
                track, pair = item.split(":", 1)
                key, value = pair.split("=", 1)
            except ValueError as exc:
                raise CommandError(f"Bad --set '{item}'; use TRACK:KEY=VALUE") from exc
            overrides.setdefault(track, {})[key] = value

        tracks = options["track"] or list(DEFAULT_RULES)
        with transaction.atomic():
            for track in tracks:
                versions = DEFAULT_RULES.get(track)
                if not versions:
                    raise CommandError(f"Unknown pathway '{track}'.")
                for index, (version_start, values) in enumerate(versions):
                    nxt = versions[index + 1][0] if index + 1 < len(versions) else None
                    effective_from = max(version_start, start)
                    effective_to = nxt - timedelta(days=1) if nxt else None
                    if effective_to and effective_to < effective_from:
                        continue
                    rule_set = ComplianceRuleSet.objects.create(
                        name=f"{options['name']} ({effective_from.year}+)" if len(versions) > 1 else options["name"],
                        effective_from=effective_from,
                        effective_to=effective_to,
                        supervision_track=track,
                    )
                    for key, value in {**values, **overrides.get(track, {})}.items():
                        ComplianceRuleParameter.objects.create(rule_set=rule_set, key=key, value=str(value))
                    self.stdout.write(f"{track}: {rule_set.name} from {effective_from} to {effective_to or 'open'}")
        self.stdout.write(self.style.SUCCESS("Approved rule sets recorded."))
