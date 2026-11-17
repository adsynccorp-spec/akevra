"""Archives completed sessions that were saved twice by one submission (a double click or two tabs before the
duplicate check was serialized). Only exact copies qualify: same supervisee, relationship, date, times, length,
type, method, author and documentation, created within 60 seconds of the first. The earliest stays; each copy
is archived (never deleted), so it no longer counts toward requirements and its row and audit history remain."""

from datetime import timedelta

from django.db import migrations
from django.utils import timezone

FIELDS = ("cycle_id", "occurred_on", "start_time", "end_time", "duration_minutes", "session_type", "meeting_method",
          "documented_by_id", "client_observation", "topics", "activities", "feedback", "follow_up", "notes")


def forwards(apps, schema_editor):
    schema_editor.execute("SELECT set_config('app.rls_bypass', 'on', true)")
    Session = apps.get_model("supervision", "SupervisionSession")
    AuditEvent = apps.get_model("audit", "AuditEvent")
    groups = {}
    for session in Session.objects.filter(status="completed", record_status="active").order_by("created_at"):
        groups.setdefault(tuple(getattr(session, f) for f in FIELDS), []).append(session)
    now = timezone.now()
    for sessions in groups.values():
        first = sessions[0]
        for copy in sessions[1:]:
            if copy.created_at - first.created_at > timedelta(seconds=60):
                continue
            copy.record_status = "archived"
            copy.archived_at = now
            copy.save(update_fields=["record_status", "archived_at", "updated_at"])
            AuditEvent.objects.create(
                organization_id=copy.organization_id, action="session.archive_duplicate",
                entity_type="supervision_session", entity_id=str(copy.id),
                before={"record_status": "active"}, after={"record_status": "archived"},
                metadata={"reason": "Exact copy saved by the same submission as the kept record",
                          "kept_session_id": str(first.id), "source": "migration 0026"},
            )


class Migration(migrations.Migration):
    dependencies = [("supervision", "0025_session_documentation"), ("audit", "0001_initial")]
    operations = [migrations.RunPython(forwards, migrations.RunPython.noop)]
