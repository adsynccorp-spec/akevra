"""Archives open appointments that duplicate another open appointment for the same two people at exactly the same
time (created before bookings were serialized: a double submission, or a single booking on top of a weekly series).
Per group one is kept, in this order of preference: the one a session was documented from, the one with a
preparation note, a confirmed one, one in a weekly series, the earliest created. The others are archived (never
deleted): they keep their history and an audit event, but no longer appear or block the time. Appointments that
were cancelled, held or marked no-show are never touched."""

from django.db import migrations
from django.utils import timezone

OPEN = ("scheduled", "confirmed")


def forwards(apps, schema_editor):
    schema_editor.execute("SELECT set_config('app.rls_bypass', 'on', true)")
    Appointment = apps.get_model("supervision", "Appointment")
    Session = apps.get_model("supervision", "SupervisionSession")
    PrepNote = apps.get_model("supervision", "PreSupervisionNote")
    AuditEvent = apps.get_model("audit", "AuditEvent")
    groups = {}
    rows = Appointment.objects.filter(record_status="active", status__in=OPEN).select_related("relationship")
    for a in rows.order_by("created_at"):
        key = (a.relationship.supervisor_id, a.relationship.supervisee_id, a.starts_at, a.ends_at)
        groups.setdefault(key, []).append(a)
    now = timezone.now()
    for items in groups.values():
        if len(items) < 2:
            continue
        with_session = set(Session.objects.filter(appointment__in=items).values_list("appointment_id", flat=True))
        with_prep = set(PrepNote.objects.filter(appointment__in=items).values_list("appointment_id", flat=True))

        def rank(a):
            return (a.id not in with_session, a.id not in with_prep, a.status != "confirmed", a.series_id is None, a.created_at)

        keep = min(items, key=rank)
        for a in items:
            if a.pk == keep.pk or a.id in with_session or a.id in with_prep:
                continue
            a.record_status = "archived"
            a.archived_at = now
            a.save(update_fields=["record_status", "archived_at", "updated_at"])
            AuditEvent.objects.create(
                organization_id=a.organization_id, action="appointment.archive_duplicate", entity_type="appointment",
                entity_id=str(a.id), before={"record_status": "active"}, after={"record_status": "archived"},
                metadata={"reason": "Duplicate of another open appointment for the same people at the same time",
                          "kept_appointment_id": str(keep.id), "source": "migration 0031"},
            )


class Migration(migrations.Migration):
    dependencies = [("supervision", "0030_rating_scale_per_assessment"), ("audit", "0001_initial")]
    operations = [migrations.RunPython(forwards, migrations.RunPython.noop)]
