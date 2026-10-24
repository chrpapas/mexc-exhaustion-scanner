-- V40-compatible strategy research integration; no trading worker deployed.
-- Parent is immutable at admission so later equity changes cannot grow an old signal.
CREATE TABLE IF NOT EXISTS t100_live_vol30_parents (
  signal_id bigint PRIMARY KEY,
  symbol text NOT NULL,
  tier text NOT NULL CHECK (tier IN ('LOW_100','HIGH_225')),
  admitted_at timestamptz NOT NULL,
  admitted_equity_usdt numeric NOT NULL CHECK (admitted_equity_usdt>0),
  target_notional_usdt numeric NOT NULL CHECK (target_notional_usdt>0),
  status text NOT NULL DEFAULT 'ACTIVE' CHECK (status IN ('ACTIVE','HALTED','EXPIRED','COMPLETE')),
  created_at timestamptz NOT NULL DEFAULT now()
);
-- No new symbol entry while an existing or halted parent may own exposure.
CREATE UNIQUE INDEX IF NOT EXISTS t100_live_vol30_active_symbol
  ON t100_live_vol30_parents(symbol) WHERE status IN ('ACTIVE','HALTED');
CREATE TABLE IF NOT EXISTS t100_live_vol30_children (
  id bigserial PRIMARY KEY,
  signal_id bigint NOT NULL REFERENCES t100_live_vol30_parents(signal_id),
  child_seq int NOT NULL CHECK(child_seq>0),
  symbol text NOT NULL,
  external_oid text NOT NULL UNIQUE,
  turnover_window_end timestamptz NOT NULL,
  prior30_turnover_usdt numeric NOT NULL CHECK (prior30_turnover_usdt>=0),
  reserved_notional_usdt numeric NOT NULL CHECK(reserved_notional_usdt>0),
  volume_contracts numeric NOT NULL CHECK(volume_contracts>0),
  filled_notional_usdt numeric NOT NULL DEFAULT 0 CHECK(filled_notional_usdt>=0),
  state text NOT NULL DEFAULT 'PREPARED' CHECK(state IN
    ('PREPARED','SUBMITTING','UNKNOWN','ACK','PARTIAL','VERIFIED_FILLED','CANCELLED','HALTED')),
  exchange_order_id text,
  created_at timestamptz NOT NULL DEFAULT now(),
  updated_at timestamptz NOT NULL DEFAULT now(),
  UNIQUE(signal_id,child_seq),
  CHECK(filled_notional_usdt<=reserved_notional_usdt)
);
-- No overlapping child submissions for a parent while fill or cancellation is unknown.
CREATE UNIQUE INDEX IF NOT EXISTS t100_live_vol30_one_inflight_child
  ON t100_live_vol30_children(signal_id) WHERE state IN
    ('PREPARED','SUBMITTING','UNKNOWN','ACK','PARTIAL','HALTED');
CREATE INDEX IF NOT EXISTS t100_live_vol30_symbol_bucket
  ON t100_live_vol30_children(symbol,turnover_window_end);