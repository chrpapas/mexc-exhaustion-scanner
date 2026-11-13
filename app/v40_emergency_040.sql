-- V40 FIRST-CANARY EMERGENCY CLOSE — CREATE ONLY / EXPLICIT OWNER APPLY
--
-- This migration is NOT auto-run by Render. Operator-reviewed DBA migration.
-- Preserve every existing entry claim/guardian incident/checkpoint.
-- The new row is deliberately immutable. INSERT commits BEFORE any potential
-- close-short POST; a timeout or crash can never mint another close request
-- for the same managed position. Failed/missing exchange acknowledgements
-- require independent signed GET reconciliation, NOT retry.
--
-- Run only on the isolated mexc_exhaustion database using the table owner.
-- No owner connection string, API credential or key should enter GitHub.

CREATE TABLE public.t100_live_v40_emergency_close_claims (
    position_id BIGINT PRIMARY KEY CHECK (position_id > 0),
    entry_external_oid TEXT NOT NULL UNIQUE
      CHECK (entry_external_oid ~ '^t100v1e[a-f0-9]{20}$'),
    close_external_oid TEXT NOT NULL UNIQUE
      CHECK (close_external_oid ~ '^t100v40x[a-f0-9]{24}$'),
    original_exchange_order_id TEXT NOT NULL
      CHECK (original_exchange_order_id ~ '^[1-9][0-9]*$'),
    symbol TEXT NOT NULL CHECK (symbol ~ '^[A-Z0-9]+_USDT$'),
    contracts NUMERIC(36,18) NOT NULL CHECK (contracts > 0),
    original_notional_usdt NUMERIC(36,18) NOT NULL
      CHECK (original_notional_usdt > 0 AND original_notional_usdt <= 12.50),
    checkpoint_version INTEGER NOT NULL CHECK (checkpoint_version > 0),
    reason TEXT NOT NULL
      CHECK (reason = 'NATIVE_STOP_MISSING_DOUBLE_GET'),
    claim_status TEXT NOT NULL DEFAULT 'POST_MAY_HAVE_OCCURRED'
      CHECK (claim_status = 'POST_MAY_HAVE_OCCURRED'),
    claimed_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp()
);

CREATE FUNCTION public.t100_v40_deny_emergency_claim_change()
RETURNS trigger
LANGUAGE plpgsql
AS $$
BEGIN
  RAISE EXCEPTION 'V40_EMERGENCY_CLOSE_CLAIM_IMMUTABLE'
    USING ERRCODE = '23514';
END
$$;

CREATE TRIGGER t100_v40_emergency_claim_immutable
BEFORE UPDATE OR DELETE
  ON public.t100_live_v40_emergency_close_claims
FOR EACH ROW
EXECUTE FUNCTION public.t100_v40_deny_emergency_claim_change();

REVOKE ALL ON TABLE public.t100_live_v40_emergency_close_claims
  FROM PUBLIC;
REVOKE UPDATE, DELETE, TRUNCATE, REFERENCES, TRIGGER
  ON TABLE public.t100_live_v40_emergency_close_claims
  FROM t100_live_executor_runtime;

GRANT SELECT, INSERT
  ON TABLE public.t100_live_v40_emergency_close_claims
  TO t100_live_executor_runtime;

GRANT SELECT
  ON TABLE public.t100_live_v40_emergency_close_claims
  TO t100_v40_guardian_reader;

COMMENT ON TABLE public.t100_live_v40_emergency_close_claims IS
  'T100 V40: immutable one-time close-short attempt fence; NOT a fill or flatness certificate.';

