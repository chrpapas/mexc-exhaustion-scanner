-- UNRELEASED. DO NOT APPLY TO THE FIRST-TRADE PRODUCTION DATABASE.
-- Exactly one pending completed-Min30 decision per owned V40 position.
-- The existing immutable stop-amendment and checkpoint journals remain the
-- source of truth for actual MEXC stop mutations and venue fills.

BEGIN;
SET LOCAL lock_timeout='5s';
SET LOCAL statement_timeout='15s';
CREATE TABLE IF NOT EXISTS public.t100_v40_exit_bar_cursor (
  position_id text PRIMARY KEY CHECK (position_id ~ '^[1-9][0-9]*$'),
  entry_external_oid text NOT NULL,
  signed_fill_at_ms bigint NOT NULL CHECK (signed_fill_at_ms > 0),
  last_committed_open_ts bigint,
  pending_open_ts bigint,
  pending_status text CHECK (pending_status IS NULL OR pending_status IN (
    'KEEP_VERIFIED_NATIVE_STOP',
    'NATIVE_STOP_TIGHTENING_PROPOSED_UNAUTHORIZED'
  )),
  pending_stop_price numeric,
  pending_best_profit_pct numeric,
  pending_trail_armed boolean,
  checkpoint_version bigint NOT NULL CHECK (checkpoint_version >= 1),
  stop_order_id text NOT NULL,
  confirmed_stop_price numeric NOT NULL CHECK (confirmed_stop_price > 0),
  best_profit_pct numeric NOT NULL DEFAULT 0 CHECK (best_profit_pct >= 0),
  trail_armed boolean NOT NULL DEFAULT false,
  cursor_revision bigint NOT NULL DEFAULT 1 CHECK (cursor_revision >= 1),
  created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
  updated_at timestamptz NOT NULL DEFAULT clock_timestamp(),
  CONSTRAINT v40_exit_pending_atomic CHECK (
    (pending_open_ts IS NULL AND pending_status IS NULL
     AND pending_stop_price IS NULL
     AND pending_best_profit_pct IS NULL AND pending_trail_armed IS NULL)
    OR
    (pending_open_ts IS NOT NULL AND pending_status IS NOT NULL
     AND pending_stop_price IS NOT NULL AND pending_stop_price>0
     AND pending_best_profit_pct IS NOT NULL
     AND pending_best_profit_pct>=best_profit_pct
     AND pending_trail_armed IS NOT NULL
     AND pending_trail_armed=(pending_best_profit_pct>=14))
  ),
  CONSTRAINT v40_exit_best_armed CHECK (trail_armed = (best_profit_pct >= 14))
);
REVOKE ALL ON public.t100_v40_exit_bar_cursor FROM PUBLIC;
-- The fenced four-slot cursor writer uses this limited state machine.
-- Immutable enrollment identity (position_id, entry_external_oid,
-- signed_fill_at_ms) cannot be changed by the runtime.
GRANT SELECT, INSERT ON public.t100_v40_exit_bar_cursor
  TO t100_live_executor_runtime;
GRANT UPDATE (
  last_committed_open_ts,pending_open_ts,pending_status,
  pending_stop_price,pending_best_profit_pct,pending_trail_armed,
  checkpoint_version,stop_order_id,confirmed_stop_price,
  best_profit_pct,trail_armed,cursor_revision,updated_at
) ON public.t100_v40_exit_bar_cursor TO t100_live_executor_runtime;

CREATE FUNCTION public.t100_v40_reject_exit_cursor_delete()
RETURNS trigger LANGUAGE plpgsql AS $cursor_no_delete$
BEGIN
    RAISE EXCEPTION 'V40_EXIT_CURSOR_DELETE_FORBIDDEN' USING ERRCODE='P0001';
END;
$cursor_no_delete$;
CREATE TRIGGER t100_v40_exit_cursor_no_delete
BEFORE DELETE ON public.t100_v40_exit_bar_cursor
FOR EACH ROW EXECUTE FUNCTION public.t100_v40_reject_exit_cursor_delete();

COMMENT ON TABLE public.t100_v40_exit_bar_cursor IS
  'UNRELEASED V40 30m cursor; migration requires owner review, no live permit';
COMMIT;
