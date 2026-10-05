from datetime import date

from django.core.management.base import BaseCommand

from apps.accounts.mfa import encrypt_mfa_secret
from apps.accounts.models import CredentialType, Identity, UserAccount
from apps.organizations.models import Organization
from apps.rbac.models import OrgRole, OrganizationRoleGrant
from apps.supervision.models import SuperviseeAssignmentPurpose, SupervisionTrack, SupervisoryRelationship

DEMO_PASSWORD = "Sprint0!Akevra"
MFA_SECRET = "JBSWY3DPEHPK3PXP"
QA_SANDBOX_SLUG = "qa-sandbox"


def make_identity(email, *, mfa=False):
    ident = Identity.objects.create_user(email=email, password=DEMO_PASSWORD)
    if mfa:
        ident.mfa_enabled = True
        ident.mfa_secret_encrypted = encrypt_mfa_secret(MFA_SECRET)
        ident.save(update_fields=["mfa_enabled", "mfa_secret_encrypted"])
    return ident


def account(ident, org, first, last, credential=""):
    return UserAccount.objects.create(
        identity=ident,
        organization=org,
        email=ident.email,
        first_name=first,
        last_name=last,
        credential_type=credential,
    )


def grant(user, role):
    OrganizationRoleGrant.objects.create(
        organization=user.organization,
        user_account=user,
        role=role,
    )


class Command(BaseCommand):
    help = "Seed Sprint 0 demo organizations, identities, roles, and relationships."

    def handle(self, *args, **options):
        if Identity.objects.filter(email="admin@akevra.test").exists():
            self._ensure_milestone2_demo()
            self._ensure_qa_sandbox()
            self.stdout.write(self.style.WARNING("Sprint 0 seed already present."))
            self._print_credentials()
            return

        northshore = Organization.objects.create(name="Northshore ABA", slug="northshore-aba")
        summit = Organization.objects.create(name="Summit Behavioral", slug="summit-behavioral")

        admin_ident = make_identity("admin@akevra.test")
        admin_ns = account(admin_ident, northshore, "Avery", "Admin", CredentialType.BCBA)
        admin_sum = account(admin_ident, summit, "Avery", "Admin", CredentialType.BCBA)
        grant(admin_ns, OrgRole.ADMINISTRATOR)
        grant(admin_sum, OrgRole.ADMINISTRATOR)

        mfa_ident = make_identity("mfa@akevra.test", mfa=True)
        account(mfa_ident, northshore, "Micah", "Factor", CredentialType.RBT)

        lockout_ident = make_identity("lockout@akevra.test")
        account(lockout_ident, northshore, "Logan", "Lockout")

        jordan_i = make_identity("jordan@akevra.test")
        alex_i = make_identity("alex@akevra.test")
        morgan_i = make_identity("morgan@akevra.test")
        director_i = make_identity("director@akevra.test")
        settings_i = make_identity("settings.admin@akevra.test")
        party_i = make_identity("party.admin@akevra.test")
        summit_sup_i = make_identity("summit.supervisor@akevra.test")
        summit_rbt_i = make_identity("summit.rbt@akevra.test")
        casey_i = make_identity("casey@akevra.test")

        jordan = account(jordan_i, northshore, "Jordan", "Lee", CredentialType.BCBA)
        alex = account(alex_i, northshore, "Alex", "Rivera", CredentialType.RBT)
        morgan = account(morgan_i, northshore, "Morgan", "Chen", CredentialType.BCBA)
        director = account(director_i, northshore, "Dana", "Brooks", CredentialType.BCBA)
        settings_admin = account(settings_i, northshore, "Riley", "Pike")
        party_admin = account(party_i, northshore, "Pat", "Ng", CredentialType.RBT)
        summit_sup = account(summit_sup_i, summit, "Sam", "Ortiz", CredentialType.BCBA)
        summit_rbt = account(summit_rbt_i, summit, "Robin", "Patel", CredentialType.RBT)
        account(casey_i, northshore, "Casey", "Nguyen", CredentialType.BCABA)

        grant(director, OrgRole.CLINICAL_DIRECTOR)
        grant(settings_admin, OrgRole.ADMINISTRATOR)
        grant(party_admin, OrgRole.ADMINISTRATOR)

        SupervisoryRelationship.objects.create(
            organization=northshore,
            supervisor=jordan,
            supervisee=alex,
            supervision_track=SupervisionTrack.RBT_ONGOING,
            started_on=date(2026, 1, 15),
        )
        SupervisoryRelationship.objects.create(
            organization=northshore,
            supervisor=morgan,
            supervisee=jordan,
            supervision_track=SupervisionTrack.BCBA_PROFESSIONAL_DEVELOPMENT,
            supervisee_purpose=SuperviseeAssignmentPurpose.STRUCTURED_PROFESSIONAL_DEVELOPMENT,
            started_on=date(2026, 2, 1),
        )
        SupervisoryRelationship.objects.create(
            organization=northshore,
            supervisor=morgan,
            supervisee=party_admin,
            supervision_track=SupervisionTrack.RBT_ONGOING,
            started_on=date(2026, 3, 1),
        )
        SupervisoryRelationship.objects.create(
            organization=summit,
            supervisor=summit_sup,
            supervisee=summit_rbt,
            supervision_track=SupervisionTrack.RBT_ONGOING,
            started_on=date(2026, 1, 8),
        )

        self._ensure_qa_sandbox()
        self.stdout.write(self.style.SUCCESS("Sprint 0 seed complete."))
        self._print_credentials()

    def _ensure_milestone2_demo(self):
        """Fill in Supervision Setup demo data if this database was seeded before that milestone."""
        northshore = Organization.objects.filter(slug="northshore-aba").first()
        if northshore is None:
            return

        if not Identity.objects.filter(email="casey@akevra.test").exists():
            ident = Identity.objects.create_user(email="casey@akevra.test", password=DEMO_PASSWORD)
            UserAccount.objects.create(
                identity=ident,
                organization=northshore,
                email=ident.email,
                first_name="Casey",
                last_name="Nguyen",
                credential_type=CredentialType.BCABA,
            )
            self.stdout.write(self.style.SUCCESS("Added casey@akevra.test (BCaBA supervisee population)."))

        updated = SupervisoryRelationship.objects.filter(
            supervisee__credential_type=CredentialType.BCBA,
            supervisee_purpose="",
        ).update(
            supervisee_purpose=SuperviseeAssignmentPurpose.STRUCTURED_PROFESSIONAL_DEVELOPMENT,
            supervision_track=SupervisionTrack.BCBA_PROFESSIONAL_DEVELOPMENT,
        )
        if updated:
            self.stdout.write(self.style.SUCCESS(f"Backfilled DEC-058 purpose on {updated} relationship(s)."))

    def _ensure_qa_sandbox(self):
        """A separate organization for client QA: create and edit freely here; the
        Northshore / Summit data stays untouched as the reference demo."""
        if Organization.objects.filter(slug=QA_SANDBOX_SLUG).exists():
            return
        sandbox = Organization.objects.create(name="QA Sandbox", slug=QA_SANDBOX_SLUG)
        admin = account(make_identity("qa.admin@akevra.test"), sandbox, "Quinn", "Admin")
        supervisor = account(make_identity("qa.bcba@akevra.test"), sandbox, "Blake", "Supervisor", CredentialType.BCBA)
        supervisee = account(make_identity("qa.rbt@akevra.test"), sandbox, "Sky", "Supervisee", CredentialType.RBT)
        account(make_identity("qa.rbt2@akevra.test"), sandbox, "Jamie", "Trainee", CredentialType.RBT)
        grant(admin, OrgRole.ADMINISTRATOR)
        SupervisoryRelationship.objects.create(
            organization=sandbox,
            supervisor=supervisor,
            supervisee=supervisee,
            supervision_track=SupervisionTrack.RBT_ONGOING,
            started_on=date(2026, 9, 1),
        )
        self.stdout.write(self.style.SUCCESS("Added the QA Sandbox organization."))

    def _print_credentials(self):
        self.stdout.write("")
        self.stdout.write("Password for every demo account: Sprint0!Akevra")
        self.stdout.write("MFA user mfa@akevra.test TOTP secret: JBSWY3DPEHPK3PXP")
        self.stdout.write("Workspace selection demo: admin@akevra.test (Northshore + Summit)")
        self.stdout.write("DEC-057 demo: jordan@akevra.test (Supervisor of Alex, Supervisee of Morgan)")
        self.stdout.write("DEC-058 / BCaBA supervisee population: casey@akevra.test (BCaBA, not an independent Supervisor)")
        self.stdout.write("RBAC-003 party: party.admin@akevra.test  /  non-party: settings.admin@akevra.test")
        self.stdout.write("QA Sandbox: qa.admin@ (Administrator), qa.bcba@ (BCBA Supervisor), qa.rbt@ (Supervisee), qa.rbt2@ (unassigned RBT)")
