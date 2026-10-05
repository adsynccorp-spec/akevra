from pathlib import Path

from django.db import migrations


SQL_PATH = Path(__file__).resolve().parent.parent / "sql" / "rls_and_audit.sql"


def apply_sql(apps, schema_editor):
    sql = SQL_PATH.read_text()
    conn = schema_editor.connection.connection
    from psycopg import ClientCursor

    with ClientCursor(conn) as cursor:
        cursor.execute(sql)


class Migration(migrations.Migration):
    dependencies = [
        ("organizations", "0001_initial"),
        ("accounts", "0001_initial"),
        ("rbac", "0001_initial"),
        ("supervision", "0001_initial"),
        ("audit", "0001_initial"),
    ]

    operations = [
        migrations.RunPython(apply_sql, migrations.RunPython.noop),
    ]
