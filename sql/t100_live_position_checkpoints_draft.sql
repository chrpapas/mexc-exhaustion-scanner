-- DRAFT ONLY: not deployed. Independent of the T100 paper portfolio.
-- Durable recovery state for a position that was already fully reconciled.
-- This table is NOT an order journal and never authorizes an exchange mutation.
--
-- Write protocol:
--   1. exchange state is verified read-only first;
--   2. current checkpoint + append-only revision are committed atomically;
--   3. checkpoint_version advances exactly by one through CAS;
--   4. unresolved exchange mutation outcomes never advance the checkpoint.
--
-- Crash examples:
-- - fill/stop exists but checkpoint missing -> restart blocks
-- - stop mutation journal unresolved -> restart blocks
-- - stop verified at exchange but checkpoint CAS not committed -> old checkpoint
--   mismatches exchange stop and restart blocks
-- - checkpoint CLOSING before close mutation -> restart blocks until close is
--   independently verified and checkpoint becomes CLOSED.

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

-- Immutable revision history. Application code has no UPDATE/DELETE path for
-- this table. Deployment permissions should additionally revoke UPDATE/DELETE
-- from the runtime role.
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
    ON t100_live_position_checkpoint_revisions(position_id, checkpoint_version);

-- A non-flat restart may be reconstructed only from rows whose lifecycle
-- state is OPEN and whose origin order + exchange position + native stop
-- exactly reconcile. REVIEW/CLOSING/CLOSED are fail-closed.
