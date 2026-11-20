-- UNRELEASED / NOT APPLIED ON PRODUCTION.
-- V40 trade-event outbox, completely separate from guardian critical incidents.
-- No role grants here. A separately reviewed deployment migration must grant
-- the minimum INSERT/SELECT/UPDATE(delivered_at,attempts) permissions.
-- No live entry/stop mutations and no backfill of synthetic exchange evidence.

BEGIN;
CREATE TABLE IF NOT EXISTS public.t100_v40_trade_event_outbox (
  event_key text PRIMARY KEY
    CHECK (event_key ~ '^[a-f0-9]{64}$'),
  event_kind text NOT NULL CHECK (event_kind IN (
    'STAGE2_QUALIFIED','ENTRY_FILLED','NATIVE_SL_ACTIVE',
    'PROFIT_5_OBSERVED','TRAIL_ARMED','TRAIL_TIGHTENED',
    'SL_EXECUTED','EXIT_PROFIT','EXIT_NONPROFIT',
    'EMERGENCY_CLOSE','ACTION_REQUIRED'
  )),
  payload jsonb NOT NULL CHECK (jsonb_typeof(payload) = 'object'),
  attempts integer NOT NULL DEFAULT 0 CHECK (attempts >= 0),
  created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
  delivered_at timestamptz,
  CONSTRAINT v40_trade_event_delivery_requires_attempt
    CHECK (delivered_at IS NULL OR attempts > 0)
);

CREATE OR REPLACE FUNCTION public.t100_v40_trade_event_immutable()
RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
  IF NEW.event_key IS DISTINCT FROM OLD.event_key
     OR NEW.event_kind IS DISTINCT FROM OLD.event_kind
     OR NEW.payload IS DISTINCT FROM OLD.payload
     OR NEW.created_at IS DISTINCT FROM OLD.created_at
     OR NEW.attempts < OLD.attempts
     OR OLD.delivered_at IS NOT NULL
        AND NEW.delivered_at IS DISTINCT FROM OLD.delivered_at
  THEN
    RAISE EXCEPTION 'V40_TRADE_EVENT_IMMUTABLE';
  END IF;
  RETURN NEW;
END;
$$;
DROP TRIGGER IF EXISTS t100_v40_trade_event_immutable_trigger
ON public.t100_v40_trade_event_outbox;
CREATE TRIGGER t100_v40_trade_event_immutable_trigger
BEFORE UPDATE ON public.t100_v40_trade_event_outbox
FOR EACH ROW EXECUTE FUNCTION public.t100_v40_trade_event_immutable();

REVOKE ALL ON public.t100_v40_trade_event_outbox FROM PUBLIC;
COMMENT ON TABLE public.t100_v40_trade_event_outbox IS
  'UNRELEASED; verified business-event Discord outbox, not a trade permit';
COMMIT;
