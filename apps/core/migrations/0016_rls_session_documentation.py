from pathlib import Path

from django.db import migrations


SQL_PATH = Path(__file__).resolve().parent.parent / "sql" / "rls_session_documentation.sql"


def apply_sql(apps, schema_editor):
    sql = SQL_PATH.read_text()
    conn = schema_editor.connection.connection
    from psycopg import ClientCursor

    with ClientCursor(conn) as cursor:
        cursor.execute(sql)


class Migration(migrations.Migration):
    dependencies = [
        ("core", "0015_audit_trigger_actor"),
        ("supervision", "0025_session_documentation"),
    ]

    operations = [
        migrations.RunPython(apply_sql, migrations.RunPython.noop),
    ]
