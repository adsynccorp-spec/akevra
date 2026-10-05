"""PostgreSQL RLS session context helpers.

Organization isolation is enforced by FORCE ROW LEVEL SECURITY policies.
The Django app role `akevra_app` cannot bypass those policies unless the
`app.rls_bypass` GUC is explicitly set to 'on' via the `rls_bypass()`
context manager. The bypass is always LOCAL to the current transaction
so it cannot leak across requests.
"""

from contextlib import contextmanager

from django.db import connection


ROLE_NAME = "akevra_app"


# The role is created by migrations and never dropped at runtime, so it is looked up
# once per process instead of on every request.
_role_verified = False


def _set_many(values: dict, local: bool = True) -> None:
    """Set several GUCs in one round trip. Defaults to LOCAL (transaction-scoped) to prevent leaking.
    Against a remote database each statement is a network round trip, so these are batched."""
    calls = ", ".join(["set_config(%s, %s, %s)"] * len(values))
    params = []
    for key, value in values.items():
        params += [key, value or "", local]
    with connection.cursor() as cursor:
        cursor.execute(f"SELECT {calls}", params)


def _set(key: str, value: str, local: bool = True) -> None:
    """Set a GUC. Defaults to LOCAL (transaction-scoped) to prevent leaking."""
    _set_many({key: value}, local=local)


def apply_app_role() -> None:
    global _role_verified
    with connection.cursor() as cursor:
        if not _role_verified:
            cursor.execute("SELECT 1 FROM pg_roles WHERE rolname = %s", [ROLE_NAME])
            if not cursor.fetchone():
                raise RuntimeError(
                    f"Database role '{ROLE_NAME}' does not exist. "
                    "Run migrations before starting the application."
                )
            _role_verified = True
        cursor.execute(f"SET ROLE {ROLE_NAME}")


def reset_role() -> None:
    with connection.cursor() as cursor:
        cursor.execute("RESET ROLE")


def clear_tenant_context() -> None:
    _set_many({
        "app.current_organization_id": "",
        "app.current_identity_id": "",
        "app.current_user_account_id": "",
        "app.workspace_pending": "off",
        "app.rls_bypass": "off",
    })


def set_tenant_context(*, organization_id=None, identity_id=None, user_account_id=None,
                       workspace_pending=False) -> None:
    """Set per-request tenant GUCs. rls_bypass is never set here."""
    _set_many({
        "app.current_organization_id": str(organization_id) if organization_id else "",
        "app.current_identity_id": str(identity_id) if identity_id else "",
        "app.current_user_account_id": str(user_account_id) if user_account_id else "",
        "app.workspace_pending": "on" if workspace_pending else "off",
    })


@contextmanager
def tenant_context(**kwargs):
    apply_app_role()
    set_tenant_context(**kwargs)
    try:
        yield
    finally:
        clear_tenant_context()
        reset_role()


@contextmanager
def rls_bypass():
    """Narrow auth-bootstrap bypass. LOCAL ensures it never leaks past this block."""
    _set("app.rls_bypass", "on", local=True)
    try:
        yield
    finally:
        _set("app.rls_bypass", "off", local=True)
