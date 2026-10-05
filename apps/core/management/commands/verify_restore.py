import os
import subprocess
import tempfile
from pathlib import Path

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError
from django.db import connection


class Command(BaseCommand):
    help = "Take a logical dump and restore it into a verification database (Sprint 0 criterion 6)."

    def handle(self, *args, **options):
        db = settings.DATABASES["default"]
        env = os.environ.copy()
        env["PGPASSWORD"] = db.get("PASSWORD") or ""
        base_args = [
            f"--host={db['HOST']}",
            f"--port={db['PORT']}",
            f"--username={db['USER']}",
        ]
        source = db["NAME"]
        verify_name = f"{source}_restore_verify"

        dump_dir = Path(tempfile.mkdtemp(prefix="akevra-restore-"))
        dump_path = dump_dir / "akevra.dump"

        self._run(["pg_dump", "-Fc", "-d", source, "-f", str(dump_path), *base_args], env)

        subprocess.run(
            ["dropdb", "--if-exists", verify_name, *base_args],
            env=env,
            check=False,
            capture_output=True,
        )
        self._run(["createdb", verify_name, *base_args], env)
        self._run(["pg_restore", "--no-owner", "-d", verify_name, str(dump_path), *base_args], env)

        with connection.cursor() as cursor:
            cursor.execute("SELECT count(*) FROM organization")
            live_orgs = cursor.fetchone()[0]
            cursor.execute("SELECT count(*) FROM user_account")
            live_users = cursor.fetchone()[0]
            cursor.execute("SELECT count(*) FROM audit_event")
            live_audit = cursor.fetchone()[0]

        verify_counts = self._counts(verify_name, db, env)
        if verify_counts != (live_orgs, live_users, live_audit):
            raise CommandError(
                f"Restore verification failed. Live={live_orgs, live_users, live_audit} "
                f"restored={verify_counts}"
            )

        subprocess.run(["dropdb", verify_name, *base_args], env=env, check=False)
        self.stdout.write(self.style.SUCCESS(
            f"Restore verified: organizations={live_orgs}, user_accounts={live_users}, "
            f"audit_events={live_audit}. Dump: {dump_path}"
        ))
        self.stdout.write(
            "Point-in-time recovery: PostgreSQL is configured with wal_level=replica and "
            "archive_mode in docker-compose.yml. Restore a base backup and replay WAL to "
            "a recovery_target_time in staging using recovery.signal."
        )

    def _run(self, cmd, env):
        result = subprocess.run(cmd, env=env, capture_output=True, text=True)
        if result.returncode != 0 and "pg_restore" not in cmd[0]:
            raise CommandError(result.stderr or result.stdout or " ".join(cmd))
        if result.returncode != 0 and "pg_restore" in cmd[0]:
            # pg_restore returns 1 with warnings on some extension comments; check objects exist later
            self.stdout.write(self.style.WARNING(result.stderr[-500:]))
        return result

    def _counts(self, dbname, db, env):
        sql = "SELECT (SELECT count(*) FROM organization), (SELECT count(*) FROM user_account), (SELECT count(*) FROM audit_event);"
        result = subprocess.run(
            [
                "psql",
                "-d",
                dbname,
                "-t",
                "-A",
                "-F",
                ",",
                "-c",
                sql,
                f"--host={db['HOST']}",
                f"--port={db['PORT']}",
                f"--username={db['USER']}",
            ],
            env=env,
            capture_output=True,
            text=True,
            check=True,
        )
        parts = result.stdout.strip().split(",")
        return tuple(int(p) for p in parts)
