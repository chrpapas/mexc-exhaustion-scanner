-- v1.3.50: allow the Daily-Confirmed Core hard-filter trader decisions introduced in v1.3.48.
-- v1.3.49 application code could emit these values, but the DB CHECK still reflected v1.2.0.

ALTER TABLE trader_signal_decisions DROP CONSTRAINT IF EXISTS trader_signal_decisions_decision_check;
ALTER TABLE trader_signal_decisions ADD CONSTRAINT trader_signal_decisions_decision_check CHECK (decision IN (
    'accepted','ignored_busy','ignored_capacity','ignored_risk','ignored_stale','ignored_invalid',
    'ignored_duplicate_symbol','ignored_exposure',
    'ignored_daily_core_filter','ignored_missing_daily_core_data',
    'error'
));

-- Repair v1.3.49 rows that were correctly classified by the application but were downgraded
-- to decision='error' only because PostgreSQL rejected the new decision value. The original
-- CheckViolationError text contains the rejected decision in its failing-row detail.
UPDATE trader_signal_decisions
SET decision = 'ignored_daily_core_filter',
    reason = 'Daily-Confirmed Core V1 flagged unresolved 4h+1D bullish continuation risk; hard-filtered',
    decided_at = now()
WHERE decision = 'error'
  AND reason ILIKE '%trader_signal_decisions_decision_check%'
  AND reason ILIKE '%ignored_daily_core_filter%';

UPDATE trader_signal_decisions
SET decision = 'ignored_missing_daily_core_data',
    reason = 'Daily-Confirmed Core hard filter failed closed because required signal-time data was missing',
    decided_at = now()
WHERE decision = 'error'
  AND reason ILIKE '%trader_signal_decisions_decision_check%'
  AND reason ILIKE '%ignored_missing_daily_core_data%';
