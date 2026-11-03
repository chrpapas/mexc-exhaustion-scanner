-- T100 live durable state and immutable accounting.
-- CREATE-ONLY MIGRATION. Independent of t100_positions / paper accounting.
-- DO NOT APPLY as part of the read-only shadow soak.
--
-- Preconditions for a future deployment:
--   * migration 027_t100_live_order_intents.sql is already applied;
--   * the live process remains order-disabled during migration;
--   * backup / rollback procedure has been reviewed;
--   * runtime DB permissions are separately restricted for append-only tables.

CREATE TABLE IF NOT EXISTS t100_live_position_checkpoints (
    position_id TEXT PRIMARY KEY,
    symbol TEXT NOT NULL,
    external_oid TEXT NOT NULL UNIQUE,
    exchange_order_id TEXT NOT NULL UNIQUE,
    contracts NUMERIC NOT NULL CHECK (contracts > 0),
    entry_price NUMERIC NOT NULL CHECK (entry_price > 0),
    stop_order_id TEXT NOT NULL UNIQUE,
    stop_price NUMERIC NOT NULL CHECK (stop_price > 0),
    best_profit_pct NUMERIC NOT NULL CHECK (best_profit_pct >= 0),
    armed BOOLEAN NOT NULL,
    lifecycle_state TEXT NOT NULL DEFAULT 'OPEN'
        CHECK (lifecycle_state IN ('OPEN','REVIEW','CLOSING','CLOSED')),
    checkpoint_version BIGINT NOT NULL CHECK (checkpoint_version > 0),
    last_write_reason TEXT NOT NULL
        CHECK (last_write_reason IN (
            'ENTRY_PROTECTED',
            'STOP_VERIFIED',
            'REVIEW_LATCHED',
            'CLOSING_LATCHED',
            'CLOSE_VERIFIED'
        )),
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    verified_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    CHECK (
        (armed = FALSE AND best_profit_pct < 14)
        OR
        (armed = TRUE AND best_profit_pct >= 14)
    )
);

CREATE UNIQUE INDEX IF NOT EXISTS uq_t100_live_checkpoint_symbol_open
    ON t100_live_position_checkpoints(symbol)
    WHERE lifecycle_state <> 'CLOSED';

CREATE TABLE IF NOT EXISTS t100_live_position_checkpoint_revisions (
    revision_id BIGSERIAL PRIMARY KEY,
    position_id TEXT NOT NULL,
    checkpoint_version BIGINT NOT NULL CHECK (checkpoint_version > 0),
    write_reason TEXT NOT NULL
        CHECK (write_reason IN (
            'ENTRY_PROTECTED',
            'STOP_VERIFIED',
            'REVIEW_LATCHED',
            'CLOSING_LATCHED',
            'CLOSE_VERIFIED'
        )),
    symbol TEXT NOT NULL,
    external_oid TEXT NOT NULL,
    exchange_order_id TEXT NOT NULL,
    contracts NUMERIC NOT NULL CHECK (contracts > 0),
    entry_price NUMERIC NOT NULL CHECK (entry_price > 0),
    stop_order_id TEXT NOT NULL,
    stop_price NUMERIC NOT NULL CHECK (stop_price > 0),
    best_profit_pct NUMERIC NOT NULL CHECK (best_profit_pct >= 0),
    armed BOOLEAN NOT NULL,
    lifecycle_state TEXT NOT NULL
        CHECK (lifecycle_state IN ('OPEN','REVIEW','CLOSING','CLOSED')),
    verified_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE(position_id, checkpoint_version),
    CHECK (
        (armed = FALSE AND best_profit_pct < 14)
        OR
        (armed = TRUE AND best_profit_pct >= 14)
    )
);

CREATE INDEX IF NOT EXISTS idx_t100_checkpoint_revisions_position
    ON t100_live_position_checkpoint_revisions(
        position_id, checkpoint_version
    );

CREATE TABLE IF NOT EXISTS t100_live_stop_amendments (
    id BIGSERIAL PRIMARY KEY,
    external_id TEXT NOT NULL UNIQUE,
    symbol TEXT NOT NULL,
    position_id TEXT NOT NULL,
    stop_order_id TEXT NOT NULL,
    previous_stop NUMERIC NOT NULL CHECK (previous_stop > 0),
    proposed_stop NUMERIC NOT NULL
        CHECK (proposed_stop > 0 AND proposed_stop < previous_stop),
    verified_stop_order_id TEXT,
    state TEXT NOT NULL DEFAULT 'PREPARED'
        CHECK (state IN ('PREPARED','SUBMITTING','UNKNOWN','VERIFIED')),
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    CHECK (
        (state = 'VERIFIED' AND verified_stop_order_id IS NOT NULL)
        OR
        (state <> 'VERIFIED' AND verified_stop_order_id IS NULL)
    )
);

CREATE INDEX IF NOT EXISTS idx_t100_stop_amendments_unresolved
    ON t100_live_stop_amendments(state, created_at)
    WHERE state <> 'VERIFIED';

CREATE UNIQUE INDEX IF NOT EXISTS uq_t100_stop_amendments_one_pending
    ON t100_live_stop_amendments(symbol, position_id, stop_order_id)
    WHERE state <> 'VERIFIED';

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
    fill_digest_sha256 TEXT NOT NULL
        CHECK (length(fill_digest_sha256) = 64),
    strategy_fully_filled_order_complete BOOLEAN NOT NULL,
    verified_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS t100_live_order_fills (
    trade_id TEXT PRIMARY KEY,
    order_id TEXT NOT NULL
        REFERENCES t100_live_order_fill_sets(order_id),
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

CREATE TABLE IF NOT EXISTS t100_live_position_funding_sets (
    position_id TEXT PRIMARY KEY,
    total_count INTEGER NOT NULL CHECK (total_count >= 0),
    funding_digest_sha256 TEXT NOT NULL
        CHECK (length(funding_digest_sha256) = 64),
    economics_verified BOOLEAN NOT NULL CHECK (economics_verified),
    double_collection_finality_verified BOOLEAN NOT NULL
        CHECK (double_collection_finality_verified),
    verified_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS t100_live_funding_events (
    event_id TEXT PRIMARY KEY,
    position_id TEXT NOT NULL
        REFERENCES t100_live_position_funding_sets(position_id),
    symbol TEXT NOT NULL,
    position_type INTEGER NOT NULL CHECK (position_type IN (1,2)),
    position_value NUMERIC NOT NULL CHECK (position_value >= 0),
    funding_cashflow_usdt NUMERIC NOT NULL,
    rate NUMERIC NOT NULL,
    settle_time_ms BIGINT NOT NULL CHECK (settle_time_ms > 0),
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE(position_id, event_id)
);

CREATE INDEX IF NOT EXISTS idx_t100_live_funding_position_time
    ON t100_live_funding_events(position_id, settle_time_ms, event_id);

CREATE TABLE IF NOT EXISTS t100_live_final_position_accounting (
    position_id TEXT PRIMARY KEY,
    entry_order_id TEXT NOT NULL,
    close_order_id TEXT NOT NULL,
    stop_order_id TEXT NOT NULL,
    opened_contracts NUMERIC NOT NULL CHECK (opened_contracts > 0),
    closed_contracts NUMERIC NOT NULL CHECK (closed_contracts > 0),
    open_notional_usdt NUMERIC NOT NULL CHECK (open_notional_usdt > 0),
    close_notional_usdt NUMERIC NOT NULL CHECK (close_notional_usdt > 0),
    realized_gross_usdt NUMERIC NOT NULL,
    fees_usdt NUMERIC NOT NULL,
    funding_cashflow_usdt NUMERIC NOT NULL,
    realized_net_usdt NUMERIC NOT NULL,
    verified_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    CHECK (opened_contracts = closed_contracts),
    UNIQUE(entry_order_id),
    UNIQUE(close_order_id)
);

-- Append-only application policy:
-- t100_live_position_checkpoint_revisions
-- t100_live_order_fill_sets
-- t100_live_order_fills
-- t100_live_position_funding_sets
-- t100_live_funding_events
-- t100_live_final_position_accounting
--
-- Runtime-role UPDATE/DELETE revocation is intentionally not embedded here
-- because the deployment role name is environment-specific. It is a separate
-- mandatory rollout gate.
