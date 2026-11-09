-- A4 audit trail: the row-level audit trigger records who made the change.
-- Every authenticated request sets app.current_user_account_id / app.current_identity_id
-- (transaction-local GUCs, see apps/core/rls.py), so the trigger can attribute the row
-- to the acting user instead of leaving it actor-less. Outside a request (seed scripts,
-- management commands) the GUCs are empty and the actor stays NULL, as before.
-- Rows are stamped with clock_timestamp() (not the transaction start), so several changes made
-- in one transaction keep their order and can be matched to the application event that caused them.

CREATE OR REPLACE FUNCTION write_row_audit()
RETURNS trigger
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = public
AS $$
DECLARE
  org uuid;
  action text;
  actor_account uuid;
  actor_ident uuid;
BEGIN
  IF TG_TABLE_NAME = 'organization' THEN
    org := NEW.id;
  ELSE
    org := NULLIF(to_jsonb(NEW)->>'organization_id', '')::uuid;
  END IF;

  BEGIN
    actor_account := NULLIF(current_setting('app.current_user_account_id', true), '')::uuid;
    actor_ident := NULLIF(current_setting('app.current_identity_id', true), '')::uuid;
  EXCEPTION WHEN invalid_text_representation THEN
    actor_account := NULL;
    actor_ident := NULL;
  END;

  IF TG_OP = 'INSERT' THEN
    action := 'create';
    INSERT INTO audit_event (
      id, organization_id, actor_identity_id, actor_user_account_id, action, entity_type, entity_id,
      before, after, metadata, created_at, updated_at
    ) VALUES (
      gen_random_uuid(), org, actor_ident, actor_account, action, TG_TABLE_NAME, NEW.id::text,
      NULL, to_jsonb(NEW), jsonb_build_object('source', 'db_trigger'), clock_timestamp(), clock_timestamp()
    );
    RETURN NEW;
  ELSIF TG_OP = 'UPDATE' THEN
    action := 'update';
    INSERT INTO audit_event (
      id, organization_id, actor_identity_id, actor_user_account_id, action, entity_type, entity_id,
      before, after, metadata, created_at, updated_at
    ) VALUES (
      gen_random_uuid(), org, actor_ident, actor_account, action, TG_TABLE_NAME, NEW.id::text,
      to_jsonb(OLD), to_jsonb(NEW), jsonb_build_object('source', 'db_trigger'), clock_timestamp(), clock_timestamp()
    );
    RETURN NEW;
  END IF;
  RETURN NULL;
END;
$$;
