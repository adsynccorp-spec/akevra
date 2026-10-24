from django.db import migrations


def forwards(apps, schema_editor):
    Note = apps.get_model("supervision", "PreSupervisionNote")
    Revision = apps.get_model("supervision", "PreSupervisionNoteRevision")
    Relationship = apps.get_model("supervision", "SupervisoryRelationship")
    # Each existing preparation becomes version 1 of its history
    for note in Note.objects.filter(revisions__isnull=True):
        Revision.objects.create(
            organization_id=note.organization_id, note=note, version=1, answers=note.answers or {}, body=note.body,
            template_key=note.template_key, submitted_by_id=note.submitted_by_id, appointment_id=note.appointment_id,
        )
    # "By calendar month" is retired: BACB applies one standard by application date. A recorded
    # application year already decided the standard; otherwise both are tracked until it is known.
    for rel in Relationship.objects.filter(fieldwork_requirements="by_month"):
        year = rel.fieldwork_application_year
        rel.fieldwork_requirements = "both" if not year else ("bacb_2027" if year >= 2027 else "bacb_2022")
        rel.save(update_fields=["fieldwork_requirements"])


class Migration(migrations.Migration):
    dependencies = [("supervision", "0018_qa_round_m4m5")]
    operations = [migrations.RunPython(forwards, migrations.RunPython.noop)]
