-- v0.7: fixed-horizon shadow performance tracking for CONFIRMED SHORT signals.

CREATE TABLE IF NOT EXISTS shadow_trades (
    episode_id bigint PRIMARY KEY REFERENCES pump_episodes(id) ON DELETE CASCADE,
    symbol text NOT NULL REFERENCES contracts(symbol) ON DELETE CASCADE,
    confirmed_at timestamptz NOT NULL,
    entry_price double precision NOT NULL CHECK (entry_price > 0),
    current_price double precision,
    current_return_pct double precision,
    last_observed_at timestamptz,
    mfe_pct double precision NOT NULL DEFAULT 0,
    mae_pct double precision NOT NULL DEFAULT 0,
    return_1h_pct double precision,
    return_4h_pct double precision,
    return_12h_pct double precision,
    return_24h_pct double precision,
    matured_at timestamptz,
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS ix_shadow_trades_confirmed_at
    ON shadow_trades(confirmed_at DESC);

CREATE INDEX IF NOT EXISTS ix_shadow_trades_matured_at
    ON shadow_trades(matured_at DESC);

CREATE TABLE IF NOT EXISTS performance_reports (
    report_date date PRIMARY KEY,
    sent_at timestamptz NOT NULL,
    timezone text NOT NULL,
    payload jsonb NOT NULL
);

-- Backfill any v0.6 confirmed-short signals already stored before this migration.
INSERT INTO shadow_trades (episode_id, symbol, confirmed_at, entry_price)
SELECT DISTINCT ON (rs.episode_id)
       rs.episode_id,
       rs.symbol,
       rs.signaled_at,
       (rs.features->>'retest_close')::double precision
FROM run_signals rs
WHERE rs.level = 'confirmed_short'
  AND rs.episode_id IS NOT NULL
  AND rs.features ? 'retest_close'
  AND NULLIF(rs.features->>'retest_close', '') IS NOT NULL
  AND (rs.features->>'retest_close')::double precision > 0
ORDER BY rs.episode_id, rs.signaled_at ASC
ON CONFLICT (episode_id) DO NOTHING;
