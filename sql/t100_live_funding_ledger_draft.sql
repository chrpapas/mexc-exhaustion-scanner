-- DRAFT ONLY: immutable final position-scoped funding evidence.
CREATE TABLE IF NOT EXISTS t100_live_position_funding_sets (
    position_id TEXT PRIMARY KEY,
    total_count INTEGER NOT NULL CHECK (total_count >= 0),
    funding_digest_sha256 TEXT NOT NULL CHECK (length(funding_digest_sha256)=64),
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
    UNIQUE(position_id,event_id)
);

CREATE INDEX IF NOT EXISTS idx_t100_live_funding_position_time
    ON t100_live_funding_events(position_id,settle_time_ms,event_id);

-- Runtime DB role should have no UPDATE/DELETE privilege on these tables.
