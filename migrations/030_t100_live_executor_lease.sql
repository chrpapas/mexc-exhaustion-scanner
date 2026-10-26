-- CREATE-ONLY leader lease for a future T100 live executor.
-- Not used by the read-only shadow worker.
CREATE TABLE IF NOT EXISTS t100_live_executor_leases (
    lease_name TEXT PRIMARY KEY,
    holder_id TEXT NOT NULL,
    fencing_token BIGINT NOT NULL CHECK (fencing_token > 0),
    lease_expires_at TIMESTAMPTZ NOT NULL,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_t100_live_executor_lease_expiry
    ON t100_live_executor_leases(lease_expires_at);
