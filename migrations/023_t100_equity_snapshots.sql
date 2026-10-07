-- v1.4.1: persist the promoted T100_200 paper equity curve for daily reporting.

CREATE TABLE IF NOT EXISTS t100_equity_snapshots (
    snapshot_at timestamptz PRIMARY KEY,
    equity_usdt double precision NOT NULL,
    realized_equity_usdt double precision NOT NULL,
    unrealized_pnl_usdt double precision NOT NULL,
    gross_notional_usdt double precision NOT NULL,
    gross_exposure_pct double precision,
    open_positions integer NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS ix_t100_equity_snapshots_time
    ON t100_equity_snapshots(snapshot_at);
