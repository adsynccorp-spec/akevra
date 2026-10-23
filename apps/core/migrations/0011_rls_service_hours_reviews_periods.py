from pathlib import Path

from django.db import migrations


SQL_PATH = Path(__file__).resolve().parent.parent / "sql" / "rls_service_hours_reviews_periods.sql"


def apply_sql(apps, schema_editor):
    sql = SQL_PATH.read_text()
    conn = schema_editor.connection.connection
    from psycopg import ClientCursor

    with ClientCursor(conn) as cursor:
        cursor.execute(sql)


class Migration(migrations.Migration):
    dependencies = [
        ("core", "0010_rls_external_supervision"),
        ("supervision", "0017_backfill_prep_and_reviews"),
    ]

    operations = [
        migrations.RunPython(apply_sql, migrations.RunPython.noop),
    ]
