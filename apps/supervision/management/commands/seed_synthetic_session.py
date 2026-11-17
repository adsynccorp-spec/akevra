from django.core.management.base import BaseCommand

from apps.core.rls import rls_bypass
from apps.supervision.synthetic import ensure_synthetic_feedback_session


class Command(BaseCommand):
    help = "Add the labelled, past-dated synthetic QA session (QA Sandbox) for testing session documentation."

    def handle(self, *args, **options):
        with rls_bypass():
            if ensure_synthetic_feedback_session(self.stdout) is None:
                self.stdout.write(self.style.WARNING("No QA Sandbox organization; nothing added."))
