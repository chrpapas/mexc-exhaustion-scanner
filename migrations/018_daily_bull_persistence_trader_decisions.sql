-- v1.3.52: promote First-Entry Trend Persistence V1 to trader/subscriber admission.
-- Preserve every previously allowed trader decision and add distinct persistence outcomes.

ALTER TABLE trader_signal_decisions DROP CONSTRAINT IF EXISTS trader_signal_decisions_decision_check;
ALTER TABLE trader_signal_decisions ADD CONSTRAINT trader_signal_decisions_decision_check CHECK (decision IN (
    'accepted','ignored_busy','ignored_capacity','ignored_risk','ignored_stale','ignored_invalid',
    'ignored_duplicate_symbol','ignored_exposure',
    'ignored_daily_core_filter','ignored_missing_daily_core_data',
    'ignored_daily_bull_persistence_filter','ignored_missing_persistence_data',
    'error'
));
