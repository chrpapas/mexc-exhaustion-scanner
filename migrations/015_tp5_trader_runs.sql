-- v1.3.6: TP5 execution strategy and idempotent paper strategy runs.

ALTER TABLE trader_runtime
    ADD COLUMN IF NOT EXISTS active_run_id text NOT NULL DEFAULT 'legacy_pre_v136';

CREATE TABLE IF NOT EXISTS trader_runs (
    run_id text PRIMARY KEY,
    mode text NOT NULL CHECK (mode IN ('paper','live')),
    strategy_name text NOT NULL,
    starting_equity_usdt double precision,
    status text NOT NULL DEFAULT 'active' CHECK (status IN ('active','archived')),
    started_at timestamptz NOT NULL DEFAULT now(),
    ended_at timestamptz,
    metadata jsonb NOT NULL DEFAULT '{}'::jsonb
);

ALTER TABLE trader_positions
    ADD COLUMN IF NOT EXISTS run_id text NOT NULL DEFAULT 'legacy_pre_v136';

CREATE INDEX IF NOT EXISTS ix_trader_positions_run_status
    ON trader_positions(run_id, status, opened_at);

ALTER TABLE trader_positions DROP CONSTRAINT IF EXISTS trader_positions_exit_strategy_check;
ALTER TABLE trader_positions ADD CONSTRAINT trader_positions_exit_strategy_check
    CHECK (exit_strategy IN (
        'ratchet_5','trailing_5','trailing_15_floor_20',
        'fixed_time_standard','tp20_or_timeout','tp5_full'
    ));

ALTER TABLE trader_positions DROP CONSTRAINT IF EXISTS trader_positions_position_maturity_check;
ALTER TABLE trader_positions ADD CONSTRAINT trader_positions_position_maturity_check
    CHECK (position_maturity IN (
        'profit_5','profit_20','1d','2d','3d','4d','5d','6d','7d','10d','14d'
    ));

INSERT INTO trader_runs(run_id, mode, strategy_name, starting_equity_usdt, status, metadata)
VALUES ('legacy_pre_v136', 'paper', 'legacy_pre_v136', NULL, 'archived', '{"migration":"015"}'::jsonb)
ON CONFLICT (run_id) DO NOTHING;
