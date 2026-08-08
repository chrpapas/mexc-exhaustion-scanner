-- v0.8: execution-risk labels for low-liquidity/high-spread signals and performance.

ALTER TABLE shadow_trades
    ADD COLUMN IF NOT EXISTS risk_tier text NOT NULL DEFAULT 'standard';

UPDATE shadow_trades st
SET risk_tier = CASE
    WHEN rs.features->>'risk_tier' IN ('standard','high_risk','extreme_risk')
        THEN rs.features->>'risk_tier'
    ELSE 'standard'
END
FROM run_signals rs
WHERE rs.episode_id = st.episode_id
  AND rs.level = 'confirmed_short';

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1
        FROM pg_constraint
        WHERE conname = 'shadow_trades_risk_tier_check'
    ) THEN
        ALTER TABLE shadow_trades
            ADD CONSTRAINT shadow_trades_risk_tier_check
            CHECK (risk_tier IN ('standard','high_risk','extreme_risk'));
    END IF;
END $$;

CREATE INDEX IF NOT EXISTS ix_shadow_trades_risk_tier
    ON shadow_trades(risk_tier, confirmed_at DESC);
