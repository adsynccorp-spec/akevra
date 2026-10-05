-- Attach prevent-delete, audit, and tenant RLS to user_invitation (RBAC-002 invitations).
-- Does not CREATE/ALTER roles — that is not permitted on Neon/Railway.
-- Invitees have no session yet, so token lookup/activation runs under the narrow
-- auth-bootstrap bypass (app.rls_bypass), exactly like login.

GRANT SELECT, INSERT, UPDATE ON user_invitation TO akevra_app;
REVOKE DELETE ON user_invitation FROM akevra_app;

DROP TRIGGER IF EXISTS trg_prevent_delete ON user_invitation;
CREATE TRIGGER trg_prevent_delete
  BEFORE DELETE ON user_invitation
  FOR EACH ROW EXECUTE FUNCTION prevent_hard_delete();

DROP TRIGGER IF EXISTS trg_row_audit ON user_invitation;
CREATE TRIGGER trg_row_audit
  AFTER INSERT OR UPDATE ON user_invitation
  FOR EACH ROW EXECUTE FUNCTION write_row_audit();

ALTER TABLE user_invitation ENABLE ROW LEVEL SECURITY;
ALTER TABLE user_invitation FORCE ROW LEVEL SECURITY;

DROP POLICY IF EXISTS tenant_isolation ON user_invitation;
CREATE POLICY tenant_isolation ON user_invitation
  FOR ALL TO akevra_app
  USING (
    current_setting('app.rls_bypass', true) = 'on'
    OR organization_id::text = current_setting('app.current_organization_id', true)
  )
  WITH CHECK (
    current_setting('app.rls_bypass', true) = 'on'
    OR organization_id::text = current_setting('app.current_organization_id', true)
  );
