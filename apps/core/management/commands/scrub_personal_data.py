"""Replace real people's emails and names in a staging database with synthetic demo data.

Every email that is not on the demo domain (@akevra.test) becomes demo.userN@akevra.test,
and the matching user accounts are renamed "Demo User N". The login keeps its password and
authenticator, so only the email used to sign in changes. Invitations sent to real addresses
get the synthetic email too, and any still pending are revoked.

Dry run by default: it only lists what would change. Pass --apply to write.
Audit history is append-only by design and keeps the previous values.
"""

from django.core.management.base import BaseCommand
from django.db import transaction

from apps.accounts.models import Identity, Invitation, InvitationStatus, UserAccount
from apps.core import rls

DEMO_DOMAIN = "@akevra.test"


class Command(BaseCommand):
    help = "Replace real emails and names with synthetic demo data (dry run unless --apply)."

    def add_arguments(self, parser):
        parser.add_argument("--apply", action="store_true", help="Write the changes (default: only list them).")

    def handle(self, *args, apply=False, **options):
        with transaction.atomic(), rls.rls_bypass():
            used = (
                set(Identity.objects.values_list("email", flat=True))
                | set(UserAccount.objects.values_list("email", flat=True))
                | set(Invitation.objects.values_list("email", flat=True))
            )
            real = sorted(email for email in used if not email.endswith(DEMO_DOMAIN))
            if not real:
                self.stdout.write(self.style.SUCCESS("No real emails found — nothing to change."))
                return

            number = 0
            for old in real:
                number += 1
                while f"demo.user{number}{DEMO_DOMAIN}" in used:
                    number += 1
                new = f"demo.user{number}{DEMO_DOMAIN}"
                used.add(new)
                self.stdout.write(f"{old}  ->  {new}  (name: Demo User {number})")
                if apply:
                    Identity.objects.filter(email=old).update(email=new)
                    UserAccount.objects.filter(email=old).update(
                        email=new, first_name="Demo", last_name=f"User {number}"
                    )
                    invitations = Invitation.objects.filter(email=old)
                    invitations.filter(status=InvitationStatus.PENDING).update(status=InvitationStatus.REVOKED)
                    invitations.update(email=new)

        if apply:
            self.stdout.write(self.style.SUCCESS(f"Replaced {len(real)} real email(s) with demo data."))
        else:
            self.stdout.write(self.style.WARNING("Dry run — nothing changed. Run again with --apply to write."))
