-- Attach prevent-delete, audit, and tenant RLS to notification.
-- Does not CREATE/ALTER roles — that is not permitted on Neon/Railway.

GRANT SELECT, INSERT, UPDATE ON notification TO akevra_app;
REVOKE DELETE ON notification FROM akevra_app;

DO $$
DECLARE
  t text;
  tables text[] := ARRAY['notification'];
BEGIN
  FOREACH t IN ARRAY tables LOOP
    EXECUTE format('DROP TRIGGER IF EXISTS trg_prevent_delete ON %I', t);
    EXECUTE format(
      'CREATE TRIGGER trg_prevent_delete BEFORE DELETE ON %I FOR EACH ROW EXECUTE FUNCTION prevent_hard_delete()',
      t
    );
    EXECUTE format('ALTER TABLE %I ENABLE ROW LEVEL SECURITY', t);
    EXECUTE format('ALTER TABLE %I FORCE ROW LEVEL SECURITY', t);
    EXECUTE format('DROP POLICY IF EXISTS tenant_isolation ON %I', t);
    EXECUTE format(
      $p$
      CREATE POLICY tenant_isolation ON %I
        FOR ALL TO akevra_app
        USING (
          current_setting('app.rls_bypass', true) = 'on'
          OR organization_id::text = current_setting('app.current_organization_id', true)
        )
        WITH CHECK (
          current_setting('app.rls_bypass', true) = 'on'
          OR organization_id::text = current_setting('app.current_organization_id', true)
        )
      $p$, t
    );
  END LOOP;
END$$;
