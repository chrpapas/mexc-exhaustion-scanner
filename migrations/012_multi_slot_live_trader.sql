-- v1.2.0: multi-slot cross portfolio trader, runner protection, breach telemetry and fees.

DROP INDEX IF EXISTS ux_trader_single_open_position;

ALTER TABLE trader_positions DROP CONSTRAINT IF EXISTS trader_positions_exit_strategy_check;
ALTER TABLE trader_positions ADD CONSTRAINT trader_positions_exit_strategy_check
    CHECK (exit_strategy IN ('ratchet_5','trailing_5','trailing_15_floor_20'));

ALTER TABLE trader_positions DROP CONSTRAINT IF EXISTS trader_positions_capital_strategy_check;
ALTER TABLE trader_positions ADD CONSTRAINT trader_positions_capital_strategy_check
    CHECK (capital_strategy IN ('isolated_full','cross_20','cross_portfolio','isolated_portfolio'));

ALTER TABLE trader_signal_decisions DROP CONSTRAINT IF EXISTS trader_signal_decisions_decision_check;
ALTER TABLE trader_signal_decisions ADD CONSTRAINT trader_signal_decisions_decision_check CHECK (decision IN (
    'accepted','ignored_busy','ignored_capacity','ignored_risk','ignored_stale','ignored_invalid',
    'ignored_duplicate_symbol','ignored_exposure','error'
));

ALTER TABLE trader_positions ADD COLUMN IF NOT EXISTS risk_tier text NOT NULL DEFAULT 'STANDARD';
ALTER TABLE trader_positions ADD COLUMN IF NOT EXISTS slot_no integer;
ALTER TABLE trader_positions ADD COLUMN IF NOT EXISTS target_20_at timestamptz;
ALTER TABLE trader_positions ADD COLUMN IF NOT EXISTS protection_armed_at timestamptz;
ALTER TABLE trader_positions ADD COLUMN IF NOT EXISTS mexc_protection_order_id bigint;
ALTER TABLE trader_positions ADD COLUMN IF NOT EXISTS breach_100_at timestamptz;
ALTER TABLE trader_positions ADD COLUMN IF NOT EXISTS breach_200_at timestamptz;
ALTER TABLE trader_positions ADD COLUMN IF NOT EXISTS breach_300_at timestamptz;
ALTER TABLE trader_positions ADD COLUMN IF NOT EXISTS breach_400_at timestamptz;
ALTER TABLE trader_positions ADD COLUMN IF NOT EXISTS entry_fee_usdt double precision NOT NULL DEFAULT 0;
ALTER TABLE trader_positions ADD COLUMN IF NOT EXISTS exit_fee_usdt double precision NOT NULL DEFAULT 0;

UPDATE trader_positions
SET risk_tier = COALESCE(NULLIF(upper(metadata->>'risk_tier'), ''), risk_tier)
WHERE risk_tier = 'STANDARD';

WITH ranked AS (
    SELECT id, row_number() OVER (ORDER BY opened_at, id) AS rn
    FROM trader_positions WHERE status='open'
)
UPDATE trader_positions p SET slot_no = ranked.rn
FROM ranked WHERE p.id=ranked.id AND p.slot_no IS NULL;

CREATE UNIQUE INDEX IF NOT EXISTS ux_trader_open_slot
    ON trader_positions(slot_no) WHERE status='open' AND slot_no IS NOT NULL;
CREATE INDEX IF NOT EXISTS ix_trader_positions_open_status
    ON trader_positions(status, opened_at);
