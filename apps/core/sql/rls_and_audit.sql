-- AKEVRA Sprint 0: app role, FORCE RLS, no-delete, immutable audit.
-- Runs after all domain tables exist.

CREATE EXTENSION IF NOT EXISTS pgcrypto;

-- akevra_app role password must be set manually after running this script:
--   ALTER ROLE akevra_app WITH PASSWORD '<new-password-from-secrets-manager>';
-- Do NOT hard-code the password here; rotate the old credential if it was committed.
DO $$
BEGIN
  IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname = 'akevra_app') THEN
    CREATE ROLE akevra_app LOGIN
      NOSUPERUSER NOCREATEDB NOCREATEROLE NOINHERIT NOBYPASSRLS;
  ELSE
    -- Harden when the database owner can; skip on Neon/Railway (no ALTER ROLE).
    BEGIN
      ALTER ROLE akevra_app
        NOSUPERUSER NOCREATEDB NOCREATEROLE NOINHERIT NOBYPASSRLS NOREPLICATION;
    EXCEPTION
      WHEN insufficient_privilege THEN
        NULL;
    END;
  END IF;
END$$;

-- Fail closed: abort migration if role still does not exist.
DO $$
BEGIN
  IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname = 'akevra_app') THEN
    RAISE EXCEPTION
      'akevra_app role could not be established — aborting migration'
      USING ERRCODE = 'insufficient_privilege';
  END IF;
END$$;

GRANT akevra_app TO CURRENT_USER;
GRANT USAGE ON SCHEMA public TO akevra_app;

GRANT SELECT, INSERT, UPDATE ON ALL TABLES IN SCHEMA public TO akevra_app;
REVOKE DELETE ON ALL TABLES IN SCHEMA public FROM akevra_app;
GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA public TO akevra_app;

ALTER DEFAULT PRIVILEGES IN SCHEMA public
  GRANT SELECT, INSERT, UPDATE ON TABLES TO akevra_app;
ALTER DEFAULT PRIVILEGES IN SCHEMA public
  REVOKE DELETE ON TABLES FROM akevra_app;

-- ---------------------------------------------------------------------------
-- Hard-delete prevention
-- ---------------------------------------------------------------------------
CREATE OR REPLACE FUNCTION prevent_hard_delete()
RETURNS trigger
LANGUAGE plpgsql
AS $$
BEGIN
  RAISE EXCEPTION 'Hard delete is not permitted (AKEVRA Sprint 0 / no-permanent-deletion)'
    USING ERRCODE = 'restrict_violation';
END;
$$;

-- ---------------------------------------------------------------------------
-- Immutable audit writer
-- ---------------------------------------------------------------------------
CREATE OR REPLACE FUNCTION write_row_audit()
RETURNS trigger
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = public
AS $$
DECLARE
  org uuid;
  action text;
BEGIN
  IF TG_TABLE_NAME = 'organization' THEN
    org := NEW.id;
  ELSE
    org := NULLIF(to_jsonb(NEW)->>'organization_id', '')::uuid;
  END IF;

  IF TG_OP = 'INSERT' THEN
    action := 'create';
    INSERT INTO audit_event (
      id, organization_id, action, entity_type, entity_id, before, after, metadata, created_at, updated_at
    ) VALUES (
      gen_random_uuid(), org, action, TG_TABLE_NAME, NEW.id::text,
      NULL, to_jsonb(NEW), jsonb_build_object('source', 'db_trigger'), now(), now()
    );
    RETURN NEW;
  ELSIF TG_OP = 'UPDATE' THEN
    action := 'update';
    INSERT INTO audit_event (
      id, organization_id, action, entity_type, entity_id, before, after, metadata, created_at, updated_at
    ) VALUES (
      gen_random_uuid(), org, action, TG_TABLE_NAME, NEW.id::text,
      to_jsonb(OLD), to_jsonb(NEW), jsonb_build_object('source', 'db_trigger'), now(), now()
    );
    RETURN NEW;
  END IF;
  RETURN NULL;
END;
$$;

CREATE OR REPLACE FUNCTION prevent_audit_mutation()
RETURNS trigger
LANGUAGE plpgsql
AS $$
BEGIN
  RAISE EXCEPTION 'Audit history is immutable'
    USING ERRCODE = 'restrict_violation';
END;
$$;

-- ---------------------------------------------------------------------------
-- Attach prevent-delete + audit triggers to domain tables
-- ---------------------------------------------------------------------------
DO $$
DECLARE
  t text;
  -- Tables where the application layer writes actor-attributed audit records.
  -- DB trigger is intentionally omitted to avoid duplicate, actor-less entries.
  app_audited text[] := ARRAY[
    'supervisory_relationship'
  ];

  tables text[] := ARRAY[
    'organization',
    'login_identity',
    'user_account',
    'auth_session',
    'organization_role_grant',
    'supervisee_intake',
    'development_plan',
    'development_plan_goal',
    'development_plan_competency',
    'development_plan_milestone',
    'monthly_cycle',
    'assignment',
    'hours_entry',
    'supervision_session',
    'appointment',
    'pre_supervision_note',
    'competency_assessment',
    'compliance_rule_set',
    'compliance_rule_parameter',
    'compliance_reminder',
    'review_comment',
    'relationship_conclusion',
    'ai_suggestion'
  ];
BEGIN
  FOREACH t IN ARRAY tables LOOP
    EXECUTE format('DROP TRIGGER IF EXISTS trg_prevent_delete ON %I', t);
    EXECUTE format(
      'CREATE TRIGGER trg_prevent_delete BEFORE DELETE ON %I FOR EACH ROW EXECUTE FUNCTION prevent_hard_delete()',
      t
    );
    -- Only attach the DB-level audit trigger to tables not covered by the app layer.
    IF NOT (t = ANY(app_audited)) THEN
      EXECUTE format('DROP TRIGGER IF EXISTS trg_row_audit ON %I', t);
      EXECUTE format(
        'CREATE TRIGGER trg_row_audit AFTER INSERT OR UPDATE ON %I FOR EACH ROW EXECUTE FUNCTION write_row_audit()',
        t
      );
    ELSE
      EXECUTE format('DROP TRIGGER IF EXISTS trg_row_audit ON %I', t);
    END IF;
  END LOOP;

  -- Also attach prevent-delete to app-audited tables (no audit trigger needed there).
  FOREACH t IN ARRAY app_audited LOOP
    EXECUTE format('DROP TRIGGER IF EXISTS trg_prevent_delete ON %I', t);
    EXECUTE format(
      'CREATE TRIGGER trg_prevent_delete BEFORE DELETE ON %I FOR EACH ROW EXECUTE FUNCTION prevent_hard_delete()',
      t
    );
    EXECUTE format('DROP TRIGGER IF EXISTS trg_row_audit ON %I', t);
  END LOOP;

  DROP TRIGGER IF EXISTS trg_prevent_delete ON audit_event;
  CREATE TRIGGER trg_prevent_delete
    BEFORE DELETE ON audit_event
    FOR EACH ROW EXECUTE FUNCTION prevent_hard_delete();

  DROP TRIGGER IF EXISTS trg_prevent_audit_update ON audit_event;
  CREATE TRIGGER trg_prevent_audit_update
    BEFORE UPDATE ON audit_event
    FOR EACH ROW EXECUTE FUNCTION prevent_audit_mutation();
END$$;

-- ---------------------------------------------------------------------------
-- FORCE ROW LEVEL SECURITY
-- ---------------------------------------------------------------------------
DO $$
DECLARE
  t text;
  tenant_tables text[] := ARRAY[
    'organization',
    'user_account',
    'auth_session',
    'organization_role_grant',
    'supervisory_relationship',
    'supervisee_intake',
    'development_plan',
    'development_plan_goal',
    'development_plan_competency',
    'development_plan_milestone',
    'monthly_cycle',
    'assignment',
    'hours_entry',
    'supervision_session',
    'appointment',
    'pre_supervision_note',
    'competency_assessment',
    'compliance_reminder',
    'review_comment',
    'relationship_conclusion',
    'ai_suggestion',
    'audit_event',
    'login_identity'
  ];
BEGIN
  FOREACH t IN ARRAY tenant_tables LOOP
    EXECUTE format('ALTER TABLE %I ENABLE ROW LEVEL SECURITY', t);
    EXECUTE format('ALTER TABLE %I FORCE ROW LEVEL SECURITY', t);
  END LOOP;
END$$;

-- Helper predicate fragments are inlined per table.

DROP POLICY IF EXISTS identity_self ON login_identity;
CREATE POLICY identity_self ON login_identity
  FOR ALL TO akevra_app
  USING (
    current_setting('app.rls_bypass', true) = 'on'
    OR id::text = current_setting('app.current_identity_id', true)
  )
  WITH CHECK (
    current_setting('app.rls_bypass', true) = 'on'
    OR id::text = current_setting('app.current_identity_id', true)
  );

DROP POLICY IF EXISTS org_isolation ON organization;
CREATE POLICY org_isolation ON organization
  FOR ALL TO akevra_app
  USING (
    current_setting('app.rls_bypass', true) = 'on'
    OR id::text = current_setting('app.current_organization_id', true)
    OR (
      current_setting('app.workspace_pending', true) = 'on'
      AND EXISTS (
        SELECT 1 FROM user_account ua
        WHERE ua.organization_id = organization.id
          AND ua.identity_id::text = current_setting('app.current_identity_id', true)
      )
    )
  )
  WITH CHECK (
    current_setting('app.rls_bypass', true) = 'on'
    OR id::text = current_setting('app.current_organization_id', true)
  );

DROP POLICY IF EXISTS tenant_user_account ON user_account;
CREATE POLICY tenant_user_account ON user_account
  FOR ALL TO akevra_app
  USING (
    current_setting('app.rls_bypass', true) = 'on'
    OR organization_id::text = current_setting('app.current_organization_id', true)
    OR (
      current_setting('app.workspace_pending', true) = 'on'
      AND identity_id::text = current_setting('app.current_identity_id', true)
    )
  )
  WITH CHECK (
    current_setting('app.rls_bypass', true) = 'on'
    OR organization_id::text = current_setting('app.current_organization_id', true)
  );

DROP POLICY IF EXISTS tenant_auth_session ON auth_session;
CREATE POLICY tenant_auth_session ON auth_session
  FOR ALL TO akevra_app
  USING (
    current_setting('app.rls_bypass', true) = 'on'
    OR identity_id::text = current_setting('app.current_identity_id', true)
  )
  WITH CHECK (
    current_setting('app.rls_bypass', true) = 'on'
    OR identity_id::text = current_setting('app.current_identity_id', true)
  );

DROP POLICY IF EXISTS tenant_audit_event ON audit_event;
CREATE POLICY tenant_audit_event ON audit_event
  FOR ALL TO akevra_app
  USING (
    current_setting('app.rls_bypass', true) = 'on'
    OR organization_id::text = current_setting('app.current_organization_id', true)
    OR (
      organization_id IS NULL
      AND (
        actor_identity_id::text = current_setting('app.current_identity_id', true)
        OR current_setting('app.workspace_pending', true) = 'on'
      )
    )
  )
  WITH CHECK (
    current_setting('app.rls_bypass', true) = 'on'
    OR organization_id::text = current_setting('app.current_organization_id', true)
    OR organization_id IS NULL
  );

DO $$
DECLARE
  t text;
  org_tables text[] := ARRAY[
    'organization_role_grant',
    'supervisory_relationship',
    'supervisee_intake',
    'development_plan',
    'development_plan_goal',
    'development_plan_competency',
    'development_plan_milestone',
    'monthly_cycle',
    'assignment',
    'hours_entry',
    'supervision_session',
    'appointment',
    'pre_supervision_note',
    'competency_assessment',
    'compliance_reminder',
    'review_comment',
    'relationship_conclusion',
    'ai_suggestion'
  ];
BEGIN
  FOREACH t IN ARRAY org_tables LOOP
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
