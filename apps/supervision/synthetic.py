"""A clearly labelled, past-dated synthetic session for client QA of the session documentation workflow.

It lives in its own relationship (Blake Supervisor with a synthetic supervisee) in the QA Sandbox, so testing
drafting, approval, sharing and acknowledgment never creates conflicting documentation on a real relationship."""

from datetime import date, time

from django.db import transaction

SYNTHETIC_EMAIL = "qa.synthetic@akevra.test"
SESSION_DAY = date(2026, 10, 21)
LABEL = "SYNTHETIC TEST DATA"


def ensure_synthetic_feedback_session(stdout=None):
    from apps.accounts.models import CredentialType, Identity, UserAccount
    from apps.organizations.models import Organization
    from apps.supervision.models import SessionStatus, SupervisionSession, SupervisionTrack, SupervisoryRelationship
    from apps.supervision.monthly import cycle_for_date

    sandbox = Organization.objects.filter(slug="qa-sandbox").first()
    supervisor = sandbox and UserAccount.objects.filter(organization=sandbox, email="qa.bcba@akevra.test").first()
    if not supervisor:
        return None
    with transaction.atomic():
        identity = Identity.objects.filter(email=SYNTHETIC_EMAIL).first() or Identity.objects.create_user(
            email=SYNTHETIC_EMAIL, password="Sprint0!Akevra")
        supervisee = UserAccount.objects.filter(organization=sandbox, email=SYNTHETIC_EMAIL).first() or UserAccount.objects.create(
            identity=identity, organization=sandbox, email=SYNTHETIC_EMAIL, first_name="Synthetic",
            last_name="Test Supervisee", credential_type=CredentialType.RBT)
        relationship = SupervisoryRelationship.objects.filter(supervisor=supervisor, supervisee=supervisee).first()
        if relationship is None:
            relationship = SupervisoryRelationship.objects.create(
                organization=sandbox, supervisor=supervisor, supervisee=supervisee,
                supervision_track=SupervisionTrack.RBT_ONGOING, started_on=date(2026, 9, 1))
        session = SupervisionSession.objects.filter(cycle__relationship=relationship, occurred_on=SESSION_DAY).first()
        if session is None:
            session = SupervisionSession.objects.create(
                organization=sandbox, cycle=cycle_for_date(relationship, SESSION_DAY), occurred_on=SESSION_DAY,
                start_time=time(10, 0), end_time=time(11, 0), duration_minutes=60, session_type="individual",
                meeting_method="in_person", client_observation=True, documented_by=supervisor,
                status=SessionStatus.COMPLETED,
                topics=f"{LABEL}. Discussed adding a visual schedule to the morning routine (discussed only; not yet approved).",
                activities=f"{LABEL}. Observed discrete trial teaching; modelled error correction, then role-play with feedback (BST).",
                feedback=f"{LABEL}. Prompting was accurate and well-timed; data were recorded after the session instead of trial by trial.",
                follow_up=f"{LABEL}. Record data within each trial for the next three sessions; read the error-correction procedure.",
                notes=f"{LABEL}: a synthetic record for QA verification only. It is not a real supervision event.",
            )
    if stdout:
        stdout.write(f"Synthetic session {session.id} on {SESSION_DAY} for {supervisee.full_name} ({SYNTHETIC_EMAIL}).")
    return session
