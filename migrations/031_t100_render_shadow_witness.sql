-- T100 Render shadow observation witness: reviewed, CREATE-ONLY schema.
-- NOT part of 027-030 trade-table migrations; NEVER auto-applied by workers.
-- Run only after backing up/reviewing existing Render Postgres and granting
-- separate minimal roles. Does not authorize execution or create orders.
CREATE TABLE IF NOT EXISTS public.t100_shadow_witness (
    scope_sha256 char(64) PRIMARY KEY
        CHECK (scope_sha256 ~ '^[a-f0-9]{64}$'),
    revision bigint NOT NULL CHECK (revision > 0),
    run_id varchar(32) NOT NULL CHECK (run_id ~ '^[a-f0-9]{12,32}$'),
    cycle_index bigint NOT NULL CHECK (cycle_index >= 0),
    observed_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    state varchar(48) NOT NULL CHECK (
        state IN ('OBSERVING_KILL_LATCHED', 'OBSERVING_NON_AUTHORIZING',
                  'SHADOW_RUNTIME_OBSERVATION_FAILED')
    ),
    kill_latched boolean NOT NULL DEFAULT false,
    kill_reason varchar(64),
    entry_allowed boolean NOT NULL DEFAULT false CHECK (entry_allowed = false),
    live_authorization boolean NOT NULL DEFAULT false CHECK (live_authorization = false),
    CHECK ((kill_latched AND kill_reason IS NOT NULL)
         OR (NOT kill_latched AND kill_reason IS NULL))
);

-- Guard against a forgotten WHERE clause or ad-hoc update silently clearing
-- the observed protection-loss latch. Only operator-reviewed separate
-- migrations can change a historical alarm state.
CREATE OR REPLACE FUNCTION public.t100_shadow_keep_kill_latched()
RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
  IF OLD.kill_latched AND
     (NOT NEW.kill_latched OR NEW.kill_reason IS DISTINCT FROM OLD.kill_reason) THEN
    RAISE EXCEPTION 'T100_SHADOW_WITNESS_KILL_IMMUTABLE';
  END IF;
  IF NEW.revision <= OLD.revision THEN
    RAISE EXCEPTION 'T100_SHADOW_WITNESS_REVISION_NONMONOTONIC';
  END IF;
  IF NEW.observed_at < OLD.observed_at THEN
    RAISE EXCEPTION 'T100_SHADOW_WITNESS_CLOCK_REGRESSION';
  END IF;
  RETURN NEW;
END;
$$;

DROP TRIGGER IF EXISTS t100_shadow_witness_immutability
ON public.t100_shadow_witness;
CREATE TRIGGER t100_shadow_witness_immutability
BEFORE UPDATE ON public.t100_shadow_witness
FOR EACH ROW EXECUTE FUNCTION public.t100_shadow_keep_kill_latched();

-- DO NOT GRANT CREATE/DELETE/TRUNCATE to runtime roles.
-- REVIEW explicit roles separately; never share live-trader write access.
-- Writer: GRANT SELECT,INSERT,UPDATE on public.t100_shadow_witness.
-- Sentinel: GRANT SELECT only on public.t100_shadow_witness.
