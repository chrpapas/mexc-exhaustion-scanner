-- v0.8.6: extend fixed-horizon shadow performance from 24h to 48h and 72h.

ALTER TABLE shadow_trades
    ADD COLUMN IF NOT EXISTS return_48h_pct double precision,
    ADD COLUMN IF NOT EXISTS return_72h_pct double precision,
    ADD COLUMN IF NOT EXISTS matured_48h_at timestamptz,
    ADD COLUMN IF NOT EXISTS matured_72h_at timestamptz;

CREATE INDEX IF NOT EXISTS ix_shadow_trades_matured_48h_at
    ON shadow_trades(matured_48h_at DESC);

CREATE INDEX IF NOT EXISTS ix_shadow_trades_matured_72h_at
    ON shadow_trades(matured_72h_at DESC);
