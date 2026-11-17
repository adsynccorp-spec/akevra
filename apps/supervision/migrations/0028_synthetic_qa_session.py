"""Adds the labelled synthetic QA session to the QA Sandbox (only where the sandbox exists). Uses the current
models on purpose: it is a one-off seed for client QA, idempotent, and a no-op elsewhere."""

from django.db import migrations


def forwards(apps, schema_editor):
    schema_editor.execute("SELECT set_config('app.rls_bypass', 'on', true)")
    from apps.supervision.synthetic import ensure_synthetic_feedback_session

    ensure_synthetic_feedback_session()


class Migration(migrations.Migration):
    dependencies = [("supervision", "0027_ai_suggestion_noted")]
    operations = [migrations.RunPython(forwards, migrations.RunPython.noop)]
