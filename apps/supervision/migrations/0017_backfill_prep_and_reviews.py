from django.db import migrations


def forwards(apps, schema_editor):
    Note = apps.get_model("supervision", "PreSupervisionNote")
    Record = apps.get_model("supervision", "ServiceHoursRecord")
    Review = apps.get_model("supervision", "ServiceHoursReview")
    for note in Note.objects.filter(relationship__isnull=True, appointment__isnull=False).select_related("appointment"):
        note.relationship_id = note.appointment.relationship_id
        note.submitted_at = note.submitted_at or note.updated_at
        note.save(update_fields=["relationship", "submitted_at"])
    # A single "reviewed by" becomes that reviewer's own review
    for record in Record.objects.filter(reviewed_by__isnull=False):
        Review.objects.get_or_create(
            record=record, reviewer_id=record.reviewed_by_id,
            defaults={"organization_id": record.organization_id},
        )


class Migration(migrations.Migration):
    dependencies = [("supervision", "0016_qa_round_structural")]
    operations = [migrations.RunPython(forwards, migrations.RunPython.noop)]
