from pathlib import Path

from django.db import migrations


SQL_PATH = Path(__file__).resolve().parent.parent / "sql" / "rls_user_invitation.sql"


def apply_sql(apps, schema_editor):
    sql = SQL_PATH.read_text()
    conn = schema_editor.connection.connection
    from psycopg import ClientCursor

    with ClientCursor(conn) as cursor:
        cursor.execute(sql)


class Migration(migrations.Migration):
    dependencies = [
        ("core", "0003_rls_supervision_setup"),
        ("accounts", "0002_invitation"),
    ]

    operations = [
        migrations.RunPython(apply_sql, migrations.RunPython.noop),
    ]
