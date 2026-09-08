-- v1.3.56: promote Trend Persistence V2.
-- V2 preserves the frozen V1 early-continuation veto and adds the separately
-- attributable Mature-Run Weak-Breakdown V1 hard-skip branch.

ALTER TABLE trader_signal_decisions DROP CONSTRAINT IF EXISTS trader_signal_decisions_decision_check;
ALTER TABLE trader_signal_decisions ADD CONSTRAINT trader_signal_decisions_decision_check CHECK (decision IN (
    'accepted','ignored_busy','ignored_capacity','ignored_risk','ignored_stale','ignored_invalid',
    'ignored_duplicate_symbol','ignored_exposure',
    'ignored_daily_core_filter','ignored_missing_daily_core_data',
    'ignored_daily_bull_persistence_filter','ignored_missing_persistence_data',
    'ignored_mature_run_weak_breakdown_filter',
    'error'
));
