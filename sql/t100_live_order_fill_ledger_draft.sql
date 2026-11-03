-- DRAFT ONLY: do not apply until live DB rollout is explicitly reviewed.
-- Append-only verified order-scoped fill evidence.

CREATE TABLE IF NOT EXISTS t100_live_order_fill_sets (
    order_id TEXT PRIMARY KEY,
    position_id TEXT NOT NULL,
    symbol TEXT NOT NULL,
    side TEXT NOT NULL CHECK (side IN ('OPEN_SHORT','CLOSE_SHORT')),
    order_state INTEGER NOT NULL CHECK (order_state IN (3,4,5)),
    deal_volume NUMERIC NOT NULL CHECK (deal_volume > 0),
    average_price NUMERIC NOT NULL CHECK (average_price > 0),
    fee_currency TEXT NOT NULL CHECK (fee_currency = 'USDT'),
    fill_count INTEGER NOT NULL CHECK (fill_count > 0),
    fill_digest_sha256 TEXT NOT NULL CHECK (length(fill_digest_sha256)=64),
    strategy_fully_filled_order_complete BOOLEAN NOT NULL,
    verified_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS t100_live_order_fills (
    trade_id TEXT PRIMARY KEY,
    order_id TEXT NOT NULL REFERENCES t100_live_order_fill_sets(order_id),
    position_id TEXT NOT NULL,
    side TEXT NOT NULL CHECK (side IN ('OPEN_SHORT','CLOSE_SHORT')),
    contracts NUMERIC NOT NULL CHECK (contracts > 0),
    price NUMERIC NOT NULL CHECK (price > 0),
    fee_usdt NUMERIC NOT NULL,
    timestamp_ms BIGINT NOT NULL CHECK (timestamp_ms > 0),
    category INTEGER NOT NULL CHECK (category = 1),
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE(order_id, trade_id)
);

CREATE INDEX IF NOT EXISTS idx_t100_live_order_fills_position_time
    ON t100_live_order_fills(position_id, timestamp_ms, trade_id);

-- Runtime DB role should have no UPDATE/DELETE privilege on these tables.
