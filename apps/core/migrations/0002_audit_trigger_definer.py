from django.db import migrations


class Migration(migrations.Migration):
    dependencies = [
        ("core", "0001_rls_and_audit"),
    ]

    operations = [
        migrations.RunSQL(
            "ALTER FUNCTION write_row_audit() SECURITY DEFINER SET search_path = public;",
            "ALTER FUNCTION write_row_audit() SECURITY INVOKER;",
        ),
    ]
