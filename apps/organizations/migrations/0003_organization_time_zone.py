from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [("organizations", "0002_require_external_reporting")]
    operations = [
        migrations.AddField(
            model_name="organization",
            name="time_zone",
            field=models.CharField(default="America/New_York", max_length=64),
        ),
    ]
