-- v1.2.6: internal research feature snapshots + bounded 15m post-signal paths.
-- Runtime path backfill is deliberately batched; this migration only creates
-- the schema and performs the small confirmed-signal feature snapshot backfill.

CREATE TABLE IF NOT EXISTS research_signal_features (
    episode_id bigint PRIMARY KEY REFERENCES pump_episodes(id) ON DELETE CASCADE,
    symbol text NOT NULL REFERENCES contracts(symbol) ON DELETE CASCADE,
    confirmed_at timestamptz NOT NULL,
    entry_price double precision,
    risk_tier text,
    run_score integer,
    exhaustion_score integer,
    episode_started_at timestamptz,
    peak_at timestamptz,
    breakdown_at timestamptz,
    retest_at timestamptz,
    feature_snapshot jsonb NOT NULL DEFAULT '{}'::jsonb,
    reasons jsonb NOT NULL DEFAULT '[]'::jsonb,
    created_at timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS ix_research_signal_features_time
    ON research_signal_features(confirmed_at DESC);
CREATE INDEX IF NOT EXISTS ix_research_signal_features_risk_time
    ON research_signal_features(risk_tier, confirmed_at DESC);

CREATE OR REPLACE VIEW research_signal_features_enriched AS
SELECT
    f.*,
    EXTRACT(EPOCH FROM (f.breakdown_at - f.episode_started_at)) / 3600.0
        AS hours_run_to_breakdown,
    EXTRACT(EPOCH FROM (f.retest_at - f.breakdown_at)) / 3600.0
        AS hours_breakdown_to_retest,
    EXTRACT(EPOCH FROM (f.confirmed_at - f.breakdown_at)) / 3600.0
        AS hours_breakdown_to_confirmation,
    EXTRACT(EPOCH FROM (f.confirmed_at - f.episode_started_at)) / 3600.0
        AS hours_episode_to_confirmation
FROM research_signal_features f;

CREATE TABLE IF NOT EXISTS research_signal_path_15m (
    episode_id bigint NOT NULL REFERENCES pump_episodes(id) ON DELETE CASCADE,
    symbol text NOT NULL REFERENCES contracts(symbol) ON DELETE CASCADE,
    candle_close_at timestamptz NOT NULL,
    open double precision NOT NULL,
    high double precision NOT NULL,
    low double precision NOT NULL,
    close double precision NOT NULL,
    volume double precision NOT NULL,
    amount double precision NOT NULL,
    close_return_pct double precision NOT NULL,
    favorable_return_pct double precision NOT NULL,
    adverse_return_pct double precision NOT NULL,
    btc_close double precision,
    created_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (episode_id, candle_close_at)
);

CREATE INDEX IF NOT EXISTS ix_research_signal_path_symbol_time
    ON research_signal_path_15m(symbol, candle_close_at DESC);
CREATE INDEX IF NOT EXISTS ix_research_signal_path_close_time
    ON research_signal_path_15m(candle_close_at DESC);

-- Expensive cumulative analytics are computed only when queried, not during
-- the scanner hot path. For shorts, favorable/adverse return use each candle's
-- low/high respectively.
CREATE OR REPLACE VIEW research_signal_path_15m_enriched AS
SELECT
    p.*,
    EXTRACT(EPOCH FROM (p.candle_close_at - f.confirmed_at)) / 60.0
        AS minutes_since_signal,
    MAX(p.favorable_return_pct) OVER w AS mfe_pct,
    MIN(p.adverse_return_pct) OVER w AS mae_pct,
    MAX(p.close_return_pct) OVER w AS best_close_return_pct,
    MIN(p.close_return_pct) OVER w AS worst_close_return_pct,
    MAX(p.close_return_pct) OVER w - p.close_return_pct AS giveback_from_best_pct,
    p.close_return_pct - MIN(p.close_return_pct) OVER w AS rebound_from_worst_pct,
    CASE
        WHEN FIRST_VALUE(p.btc_close) OVER w IS NOT NULL
         AND FIRST_VALUE(p.btc_close) OVER w > 0
         AND p.btc_close IS NOT NULL
        THEN p.btc_close / FIRST_VALUE(p.btc_close) OVER w - 1.0
        ELSE NULL
    END AS btc_return_since_signal_pct
FROM research_signal_path_15m p
JOIN research_signal_features f USING (episode_id)
WINDOW w AS (
    PARTITION BY p.episode_id
    ORDER BY p.candle_close_at
    ROWS BETWEEN UNBOUNDED PRECEDING AND CURRENT ROW
);

-- Small one-time backfill. The full feature JSON was already frozen inside the
-- confirmed_short run_signal, so no exchange/API calls are needed.
INSERT INTO research_signal_features (
    episode_id, symbol, confirmed_at, entry_price, risk_tier,
    run_score, exhaustion_score, episode_started_at, peak_at,
    breakdown_at, retest_at, feature_snapshot, reasons
)
SELECT DISTINCT ON (rs.episode_id)
    rs.episode_id,
    rs.symbol,
    rs.signaled_at,
    st.entry_price,
    COALESCE(st.risk_tier, rs.features->>'risk_tier'),
    COALESCE(NULLIF(rs.features->>'run_score', '')::integer, rs.score),
    NULLIF(rs.features->>'exhaustion_score', '')::integer,
    pe.started_at,
    pe.peak_at,
    COALESCE(pe.breakdown_at, NULLIF(rs.features->>'breakdown_at', '')::timestamptz),
    COALESCE(pe.retest_at, NULLIF(rs.features->>'retest_at', '')::timestamptz),
    rs.features,
    rs.reasons
FROM run_signals rs
JOIN pump_episodes pe ON pe.id = rs.episode_id
LEFT JOIN shadow_trades st ON st.episode_id = rs.episode_id
WHERE rs.level = 'confirmed_short'
  AND rs.episode_id IS NOT NULL
ORDER BY rs.episode_id, rs.signaled_at ASC
ON CONFLICT (episode_id) DO NOTHING;
