-- Independent ledger: NEVER overlap t100_positions paper accounting.
CREATE TABLE IF NOT EXISTS t100_live_order_intents (
  id bigserial PRIMARY KEY,
  signal_id bigint NOT NULL UNIQUE,
  symbol text NOT NULL,
  external_oid text NOT NULL UNIQUE,
  reference_price numeric NOT NULL CHECK(reference_price>0),
  notional_usdt numeric NOT NULL CHECK(notional_usdt>0),
  volume_contracts numeric NOT NULL CHECK(volume_contracts>0),
  stop_price numeric NOT NULL CHECK(stop_price>0),
  state text NOT NULL DEFAULT 'PREPARED' CHECK(state IN ('PREPARED','SUBMITTING','UNKNOWN','ACK','REVIEW','CLOSED')),
  exchange_order_id text,
  failure_reason text,
  created_at timestamptz NOT NULL DEFAULT now(),
  updated_at timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS t100_live_order_intents_state_idx ON t100_live_order_intents(state,created_at);
