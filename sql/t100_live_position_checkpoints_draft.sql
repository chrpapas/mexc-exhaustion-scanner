-- DRAFT ONLY: not deployed. Independent of the T100 paper portfolio.
-- Durable recovery state for a position that was already fully reconciled.
-- This table is NOT an order journal and never authorizes a mutation.
--
-- Update a checkpoint only after the corresponding exchange state has been
-- verified read-only. If a stop mutation outcome is ambiguous, leave the
-- checkpoint unchanged and keep the stop-amendment journal unresolved.

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
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    CHECK (
        (armed = FALSE AND best_profit_pct < 14)
        OR
        (armed = TRUE AND best_profit_pct >= 14)
    )
);

CREATE UNIQUE INDEX IF NOT EXISTS uq_t100_live_checkpoint_symbol_open
    ON t100_live_position_checkpoints(symbol)
    WHERE lifecycle_state <> 'CLOSED';

-- A non-flat restart may be reconstructed only from rows whose lifecycle
-- state is OPEN and whose exchange position + native stop exactly reconcile.
-- REVIEW/CLOSING/CLOSED rows are fail-closed for autonomous recovery.
