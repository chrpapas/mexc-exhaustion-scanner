-- v1.3.82: ATR Hard Filter V1 trader decision vocabulary.
-- Existing deployments have the v1.3.56 CHECK constraint, so extend it before
-- the paper trader can persist universal ATR rejects.

ALTER TABLE trader_signal_decisions DROP CONSTRAINT IF EXISTS trader_signal_decisions_decision_check;
ALTER TABLE trader_signal_decisions ADD CONSTRAINT trader_signal_decisions_decision_check CHECK (decision IN (
    'accepted','ignored_busy','ignored_capacity','ignored_risk','ignored_stale','ignored_invalid',
    'ignored_duplicate_symbol','ignored_exposure',
    'ignored_daily_core_filter','ignored_missing_daily_core_data',
    'ignored_daily_bull_persistence_filter','ignored_missing_persistence_data',
    'ignored_mature_run_weak_breakdown_filter',
    'ignored_atr_capacity_gate','ignored_missing_atr_capacity_data',
    'ignored_atr_hard_filter','ignored_missing_atr_hard_filter_data',
    'error'
));

-- Repair ATR-hard rejects that v1.3.81 could only persist as `error` because the
-- old CHECK constraint rejected the intended decision value. This also repairs
-- the already-seen KMNO-style failure without replaying/opening the signal.
UPDATE trader_signal_decisions
SET decision='ignored_atr_hard_filter'
WHERE decision='error'
  AND reason ILIKE '%ignored_atr_hard_filter%';

UPDATE trader_signal_decisions
SET decision='ignored_missing_atr_hard_filter_data'
WHERE decision='error'
  AND reason ILIKE '%ignored_missing_atr_hard_filter_data%';
