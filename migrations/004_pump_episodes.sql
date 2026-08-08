-- v0.6: persistent pump episodes and retest-confirmed shorts.

CREATE TABLE IF NOT EXISTS pump_episodes (
    id bigserial PRIMARY KEY,
    symbol text NOT NULL REFERENCES contracts(symbol) ON DELETE CASCADE,
    started_at timestamptz NOT NULL,
    updated_at timestamptz NOT NULL DEFAULT now(),
    state text NOT NULL CHECK (state IN (
        'run_watch', 'exhaustion_watch', 'breakdown_watch', 'confirmed_short'
    )),
    peak_price double precision NOT NULL CHECK (peak_price > 0),
    peak_at timestamptz NOT NULL,
    broken_level double precision,
    breakdown_at timestamptz,
    breakdown_atr_15m double precision,
    retest_at timestamptz,
    confirmed_short_at timestamptz,
    closed_at timestamptz,
    last_run_score integer NOT NULL DEFAULT 0,
    last_exhaustion_score integer NOT NULL DEFAULT 0,
    metadata jsonb NOT NULL DEFAULT '{}'::jsonb
);

CREATE UNIQUE INDEX IF NOT EXISTS ux_pump_episodes_active_symbol
    ON pump_episodes(symbol)
    WHERE closed_at IS NULL;

CREATE INDEX IF NOT EXISTS ix_pump_episodes_symbol_started
    ON pump_episodes(symbol, started_at DESC);

ALTER TABLE run_signals
    ADD COLUMN IF NOT EXISTS episode_id bigint;

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1
        FROM pg_constraint
        WHERE conname = 'run_signals_episode_id_fkey'
    ) THEN
        ALTER TABLE run_signals
            ADD CONSTRAINT run_signals_episode_id_fkey
            FOREIGN KEY (episode_id)
            REFERENCES pump_episodes(id)
            ON DELETE SET NULL;
    END IF;
END $$;

CREATE INDEX IF NOT EXISTS ix_run_signals_episode_id
    ON run_signals(episode_id);

ALTER TABLE run_signals
    DROP CONSTRAINT IF EXISTS run_signals_level_check;

ALTER TABLE run_signals
    ADD CONSTRAINT run_signals_level_check
    CHECK (level IN (
        'run_watch',
        'exhaustion_watch',
        'short_setup',
        'breakdown_watch',
        'confirmed_short'
    ));
