from django.db import migrations


def forwards(apps, schema_editor):
    MonthlyCycle = apps.get_model("supervision", "MonthlyCycle")
    Attestation = apps.get_model("supervision", "ServiceHoursAttestation")
    Record = apps.get_model("supervision", "ServiceHoursRecord")

    MonthlyCycle.objects.filter(status="active").update(status="open")

    # One shared record per supervisee and month. Where two relationships held different figures
    # for the same month, the latest becomes the active record and earlier ones are kept, superseded.
    groups = {}
    for a in Attestation.objects.filter(record_status="active").select_related("cycle__relationship").order_by("updated_at"):
        rel = a.cycle.relationship
        groups.setdefault((rel.supervisee_id, a.cycle.year, a.cycle.month), []).append((a, rel))
    for (supervisee_id, year, month), rows in groups.items():
        previous = None
        for index, (a, rel) in enumerate(rows):
            latest = index == len(rows) - 1
            record = Record.objects.create(
                organization_id=rel.organization_id,
                supervisee_id=supervisee_id,
                year=year,
                month=month,
                hours=a.hours,
                zero_reason="other" if a.hours == 0 else "",
                notes=a.notes or ("Migrated confirmed zero (reason not recorded at the time)." if a.hours == 0 else ""),
                source_type="self_attested",
                source_reference=f"service_hours_attestation:{a.id}",
                entered_by_id=a.attested_by_id,
                supersedes=previous,
                record_status="active" if latest else "superseded",
            )
            previous = record


class Migration(migrations.Migration):
    dependencies = [("supervision", "0013_period_status_shared_service_hours")]
    operations = [migrations.RunPython(forwards, migrations.RunPython.noop)]
