-- REVIEW ONLY. NEVER auto-run from an execution/guardian worker.
-- Requires prior migrations 031/032/034/035 and separately created roles:
--   t100_v40_guardian_reader          LOGIN, unique password, NOINHERIT
--   t100_v40_guardian_incident_writer LOGIN, unique password, NOINHERIT
-- Credentials must be supplied in the Render Dashboard, not this repository.
--
-- Distinct privileges: independent guardian reads exact V40 position origin;
-- incident writer has ONLY alert-state/outbox DML for a durable Discord latch.
-- Neither role can create/delete orders, change stops, or modify V40 journals.
--
-- This script must be reviewed/applied by the trusted database owner.
BEGIN;
DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_roles WHERE rolname='t100_v40_guardian_reader'
    ) OR NOT EXISTS (
        SELECT 1 FROM pg_roles WHERE rolname='t100_v40_guardian_incident_writer'
    ) THEN
        RAISE EXCEPTION 'V40_GUARDIAN_DISTINCT_ROLES_MUST_BE_PROVISIONED_FIRST';
    END IF;
    IF (
        to_regclass('public.t100_guardian_incident_state') IS NULL
        OR to_regclass('public.t100_guardian_incident_outbox') IS NULL
        OR to_regclass('public.t100_live_position_checkpoints') IS NULL
        OR to_regclass('public.t100_live_position_checkpoint_revisions') IS NULL
        OR to_regclass('public.t100_live_order_intents') IS NULL
        OR to_regclass('public.t100_live_stop_amendments') IS NULL
    ) THEN
        RAISE EXCEPTION 'V40_GUARDIAN_PREREQUISITE_MIGRATIONS_MISSING';
    END IF;
END
$$;

GRANT CONNECT ON DATABASE mexc_exhaustion
    TO t100_v40_guardian_reader,t100_v40_guardian_incident_writer;
GRANT USAGE ON SCHEMA public
    TO t100_v40_guardian_reader,t100_v40_guardian_incident_writer;

GRANT SELECT ON
    public.t100_live_position_checkpoints,
    public.t100_live_position_checkpoint_revisions,
    public.t100_live_order_intents,
    public.t100_live_stop_amendments
    TO t100_v40_guardian_reader;

-- The state INSERT/UPDATE fires the 035 trigger which INSERTs into the
-- incident outbox as the caller. WITHOUT outbox INSERT, safety alerts
-- fail at first-write despite observer GETs being healthy.
GRANT SELECT,INSERT,UPDATE ON
    public.t100_guardian_incident_state,
    public.t100_guardian_incident_outbox
    TO t100_v40_guardian_incident_writer;

DO $$
DECLARE seq_name text;
BEGIN
    seq_name=pg_get_serial_sequence(
        'public.t100_guardian_incident_outbox','seq'
    );
    IF seq_name IS NULL THEN
        RAISE EXCEPTION 'V40_GUARDIAN_IDENTITY_SEQUENCE_UNVERIFIED';
    END IF;
    EXECUTE format(
        'GRANT USAGE ON SEQUENCE %s TO t100_v40_guardian_incident_writer',
        seq_name
    );
END
$$;
COMMIT;

-- Postflight: connect separately as EACH new role, with READ ONLY
-- transaction when using the guardian reader, then verify the exact grants
-- with app.t100_v40_guardian_ledger_reader and
-- app.t100_live_guardian_durable_worker.verify_alert_writer_role.
-- Roles with unexpected inherited DML fail closed rather than bypassing.
