from pathlib import Path

from django.db import migrations


SQL_PATH = Path(__file__).resolve().parent.parent / "sql" / "rls_idp_tables.sql"


def apply_sql(apps, schema_editor):
    sql = SQL_PATH.read_text()
    conn = schema_editor.connection.connection
    from psycopg import ClientCursor

    with ClientCursor(conn) as cursor:
        cursor.execute(sql)


class Migration(migrations.Migration):
    dependencies = [
        ("core", "0002_audit_trigger_definer"),
        ("supervision", "0002_supervision_setup"),
    ]

    operations = [
        migrations.RunPython(apply_sql, migrations.RunPython.noop),
    ]
