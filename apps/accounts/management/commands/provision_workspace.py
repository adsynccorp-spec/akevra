"""Create a workspace (organization tenant) and invite its first Administrator.

Used for manual provisioning: an organization workspace, or an individual BCBA's own workspace
(--individual) where one BCBA is both Administrator and Supervisor. Invitations normally come from
an existing Administrator; a new workspace has none, so this sends the first one. The invitee
activates through the emailed link and sets up MFA as usual. If the email already has an AKEVRA login
(e.g. the BCBA also works in a subscribing organization), the same login gains the new workspace and
chooses between workspaces at sign-in; the data of each stays fully separate.

    python manage.py provision_workspace --name "Dr. Lee Supervision" --admin-email lee@example.com --individual
"""

from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from django.core.management.base import BaseCommand, CommandError
from django.db import transaction
from django.utils.text import slugify

from apps.accounts.invitations import _new_token, _expiry, _send_email, activation_url
from apps.accounts.models import CredentialType, Invitation, InvitationStatus
from apps.audit.services import write_audit
from apps.organizations.models import Organization
from apps.rbac.models import OrgRole


class Command(BaseCommand):
    help = "Create a workspace and invite its first Administrator (manual provisioning)."

    def add_arguments(self, parser):
        parser.add_argument("--name", required=True, help="Workspace name, e.g. 'Dr. Lee Supervision'")
        parser.add_argument("--admin-email", required=True)
        parser.add_argument("--slug", default="", help="Defaults to the name, slugified")
        parser.add_argument("--time-zone", default="America/New_York")
        parser.add_argument("--credential", default="", choices=["", *CredentialType.values],
                            help="The Administrator's credential (bcba for an individual BCBA)")
        parser.add_argument("--individual", action="store_true",
                            help="Individual BCBA workspace: the Administrator is a BCBA who also supervises")

    def handle(self, *args, **opts):
        email = opts["admin_email"].strip().lower()
        if "@" not in email:
            raise CommandError("Enter a valid --admin-email.")
        credential = CredentialType.BCBA if opts["individual"] else opts["credential"]
        try:
            ZoneInfo(opts["time_zone"])
        except (ZoneInfoNotFoundError, ValueError) as exc:
            raise CommandError(f"Unknown time zone {opts['time_zone']!r}.") from exc
        slug = slugify(opts["slug"] or opts["name"])[:64]
        if not slug:
            raise CommandError("The workspace needs a name that makes a usable slug.")
        if Organization.objects.filter(slug=slug).exists():
            raise CommandError(f"A workspace with the slug '{slug}' already exists. Pass a different --slug.")

        with transaction.atomic():
            org = Organization.objects.create(name=opts["name"].strip(), slug=slug, time_zone=opts["time_zone"])
            raw, token_hash = _new_token()
            invitation = Invitation.objects.create(
                organization=org, email=email, credential_type=credential, org_role=OrgRole.ADMINISTRATOR,
                token_hash=token_hash, expires_at=_expiry(), invited_by=None, status=InvitationStatus.PENDING,
            )
            write_audit(action="workspace.provisioned", entity_type="organization", entity_id=org.id, organization=org,
                        metadata={"admin_email": email, "individual": bool(opts["individual"]), "credential_type": credential})
        delivery = _send_email(invitation, raw)

        self.stdout.write(self.style.SUCCESS(f"Workspace '{org.name}' ({org.slug}) created."))
        self.stdout.write(f"First Administrator invited: {email}"
                          + (" (BCBA: Administrator + Supervisor)" if opts["individual"] else ""))
        self.stdout.write(f"Activation link: {activation_url(raw)}")
        if not delivery["email_sent"]:
            self.stdout.write(self.style.WARNING(f"Email not sent ({delivery['email_error'] or 'no email backend'}); share the link securely."))
