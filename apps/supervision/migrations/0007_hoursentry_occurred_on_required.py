from django.db import migrations, models


class Migration(migrations.Migration):
    """Separate from 0006 so the backfill's row updates commit before the column is altered."""

    dependencies = [
        ("supervision", "0006_monthly_work_and_hours"),
    ]

    operations = [
        migrations.AlterField(
            model_name="hoursentry",
            name="occurred_on",
            field=models.DateField(),
        ),
    ]
