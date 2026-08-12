-- v0.9: extend confirmed-short shadow tracking to seven days and persist
-- path events used by the theoretical 1x-isolated / 5x-cross-buffer analysis.

ALTER TABLE shadow_trades
    ADD COLUMN IF NOT EXISTS return_168h_pct double precision,
    ADD COLUMN IF NOT EXISTS matured_168h_at timestamptz,
    ADD COLUMN IF NOT EXISTS first_profit_at timestamptz,
    ADD COLUMN IF NOT EXISTS target_20_at timestamptz,
    ADD COLUMN IF NOT EXISTS isolated_100_breach_at timestamptz,
    ADD COLUMN IF NOT EXISTS cross_400_breach_at timestamptz;

CREATE INDEX IF NOT EXISTS ix_shadow_trades_matured_168h_at
    ON shadow_trades(matured_168h_at DESC);
