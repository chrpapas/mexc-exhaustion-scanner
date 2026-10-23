-- DRAFT ONLY: not deployed. Independent of paper portfolio.
-- Apply only after review of existing database migrations and rollback plan.
CREATE TABLE IF NOT EXISTS t100_live_stop_amendments (
    id BIGSERIAL PRIMARY KEY,
    external_id TEXT NOT NULL UNIQUE,
    symbol TEXT NOT NULL,
    position_id TEXT NOT NULL,
    stop_order_id TEXT NOT NULL,
    previous_stop NUMERIC NOT NULL CHECK (previous_stop > 0),
    proposed_stop NUMERIC NOT NULL CHECK (proposed_stop > 0 AND proposed_stop < previous_stop),
    state TEXT NOT NULL DEFAULT 'PREPARED'
        CHECK (state IN ('PREPARED','SUBMITTING','UNKNOWN','VERIFIED')),
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
-- Never delete unresolved records to force a new submission.
CREATE INDEX IF NOT EXISTS idx_t100_stop_amendments_unresolved
    ON t100_live_stop_amendments (state, created_at)
    WHERE state <> 'VERIFIED';
CREATE UNIQUE INDEX IF NOT EXISTS uq_t100_stop_amendments_one_pending
    ON t100_live_stop_amendments (symbol, position_id, stop_order_id)
    WHERE state <> 'VERIFIED';
