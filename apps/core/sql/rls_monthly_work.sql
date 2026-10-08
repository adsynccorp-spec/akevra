-- Attach prevent-delete, audit, and tenant RLS to the Monthly Work & Hours tables.
-- Does not CREATE/ALTER roles — that is not permitted on Neon/Railway.

GRANT SELECT, INSERT, UPDATE ON assignment_event TO akevra_app;
GRANT SELECT, INSERT, UPDATE ON assignment_attachment TO akevra_app;
GRANT SELECT, INSERT, UPDATE ON service_hours_attestation TO akevra_app;
REVOKE DELETE ON assignment_event FROM akevra_app;
REVOKE DELETE ON assignment_attachment FROM akevra_app;
REVOKE DELETE ON service_hours_attestation FROM akevra_app;

DO $$
DECLARE
  t text;
  tables text[] := ARRAY[
    'assignment_event',
    'assignment_attachment',
    'service_hours_attestation'
  ];
BEGIN
  FOREACH t IN ARRAY tables LOOP
    EXECUTE format('DROP TRIGGER IF EXISTS trg_prevent_delete ON %I', t);
    EXECUTE format(
      'CREATE TRIGGER trg_prevent_delete BEFORE DELETE ON %I FOR EACH ROW EXECUTE FUNCTION prevent_hard_delete()',
      t
    );
    EXECUTE format('DROP TRIGGER IF EXISTS trg_row_audit ON %I', t);
    EXECUTE format(
      'CREATE TRIGGER trg_row_audit AFTER INSERT OR UPDATE ON %I FOR EACH ROW EXECUTE FUNCTION write_row_audit()',
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
