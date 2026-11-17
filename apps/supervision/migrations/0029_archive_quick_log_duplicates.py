"""Archives an incomplete record that duplicates a fully documented one: a completed session with no start/end
time, no meeting method and no documentation (a Quick Log saved before Quick Logs became drafts) on the same day,
for the same supervisee and session type and with the same length as a completed record that has times. The fully
documented record stays; the incomplete one is archived (never deleted), so the event counts once and both rows
and their audit history remain."""

from django.db import migrations
from django.utils import timezone

NARRATIVE = ("topics", "activities", "feedback", "follow_up", "notes")


def forwards(apps, schema_editor):
    schema_editor.execute("SELECT set_config('app.rls_bypass', 'on', true)")
    Session = apps.get_model("supervision", "SupervisionSession")
    AuditEvent = apps.get_model("audit", "AuditEvent")
    now = timezone.now()
    active = Session.objects.filter(status="completed", record_status="active").select_related("cycle__relationship")
    for bare in active.filter(start_time__isnull=True, meeting_method=""):
        if any((getattr(bare, f) or "").strip() for f in NARRATIVE):
            continue
        kept = (active.filter(cycle__relationship__supervisee_id=bare.cycle.relationship.supervisee_id,
                              occurred_on=bare.occurred_on, session_type=bare.session_type,
                              duration_minutes=bare.duration_minutes, start_time__isnull=False)
                .exclude(pk=bare.pk).order_by("created_at").first())
        if kept is None:
            continue
        bare.record_status = "archived"
        bare.archived_at = now
        bare.save(update_fields=["record_status", "archived_at", "updated_at"])
        AuditEvent.objects.create(
            organization_id=bare.organization_id, action="session.archive_duplicate",
            entity_type="supervision_session", entity_id=str(bare.id),
            before={"record_status": "active"}, after={"record_status": "archived"},
            metadata={"reason": "Incomplete record (no times, method or documentation) duplicating a fully documented "
                                "session on the same day", "kept_session_id": str(kept.id), "source": "migration 0029"},
        )


class Migration(migrations.Migration):
    dependencies = [("supervision", "0028_synthetic_qa_session"), ("audit", "0001_initial")]
    operations = [migrations.RunPython(forwards, migrations.RunPython.noop)]
