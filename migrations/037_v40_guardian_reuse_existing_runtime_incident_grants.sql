-- Optional SIMPLE guardian setup: REUSE the existing V40 executor
-- runtime PostgreSQL login. No new database roles, no new Postgres DB.
--
-- Tradeoff: an independent guardian's DB credential can already access
-- approved immutable V40 order journals. This is weaker isolation than
-- the preferred 036 two-role deployment. Protect the Render guardian and
-- keep the separate GET-ONLY MEXC credential. THIS DOES NOT GRANT LIVE
-- ORDER PERMISSION. Do not run as a signal reader or trading agent.
--
-- Manual OWNER-APPROVED one-time migration only. Prerequisite migration
-- 035 durable incident state + outbox installed first. DO NOT execute
-- from the guardian startup or store passwords in this file.

BEGIN;

DO $v40$
BEGIN
  IF NOT EXISTS (
    SELECT 1 FROM pg_roles
     WHERE rolname='t100_live_executor_runtime'
       AND rolcanlogin
       AND NOT rolsuper
       AND NOT rolcreaterole
       AND NOT rolcreatedb
  ) THEN
    RAISE EXCEPTION 'RESTRICTED_V40_EXECUTOR_ROLE_REQUIRED';
  END IF;
  IF to_regclass('public.t100_guardian_incident_state') IS NULL
     OR to_regclass('public.t100_guardian_incident_outbox') IS NULL
  THEN
    RAISE EXCEPTION 'GUARDIAN_MIGRATION_035_REQUIRED';
  END IF;
END $v40$;

GRANT SELECT, INSERT, UPDATE
  ON TABLE public.t100_guardian_incident_state,
           public.t100_guardian_incident_outbox
  TO t100_live_executor_runtime;

DO $v40$
DECLARE sequence_name text;
BEGIN
    sequence_name := pg_get_serial_sequence(
        'public.t100_guardian_incident_outbox','seq'
    );
    IF sequence_name IS NOT NULL THEN
        EXECUTE format(
           'GRANT USAGE ON SEQUENCE %s TO t100_live_executor_runtime',
            sequence_name
        );
    ELSE
        RAISE EXCEPTION 'GUARDIAN_OUTBOX_SEQUENCE_NOT_FOUND';
    END IF;
END $v40$;

COMMIT;

-- The guardian checks current_user, exact DB, no administrative powers,
-- no DELETE/TRUNCATE on incidents, and all required incident grants
-- at every startup. Existing runtime's journal role is unchanged.
