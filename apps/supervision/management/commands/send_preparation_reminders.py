"""Remind supervisees whose upcoming supervision session has no preparation yet.

Run on a schedule (e.g. Railway cron, hourly): `python manage.py send_preparation_reminders`.
Each appointment gets at most one reminder; the Supervisor's views show Preparation Pending.
"""

from django.core.management.base import BaseCommand
from django.db import transaction

from apps.core.rls import apply_app_role, reset_role, rls_bypass
from apps.supervision.notifications import send_preparation_reminders


class Command(BaseCommand):
    help = "Send preparation reminders for sessions starting within the next N hours (default 48)."

    def add_arguments(self, parser):
        parser.add_argument("--hours", type=int, default=48, help="Look-ahead window in hours")

    def handle(self, *args, **options):
        # Runs outside a request, across every organization: the app role with the narrow bypass
        with transaction.atomic():
            apply_app_role()
            try:
                with rls_bypass():
                    sent = send_preparation_reminders(window_hours=options["hours"])
            finally:
                reset_role()
        self.stdout.write(self.style.SUCCESS(f"Sent {sent} preparation reminder(s)."))
