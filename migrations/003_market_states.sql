ALTER TABLE run_signals DROP CONSTRAINT IF EXISTS run_signals_level_check;

UPDATE run_signals
SET level = 'run_watch'
WHERE level IN ('watch', 'candidate');

ALTER TABLE run_signals
    ADD CONSTRAINT run_signals_level_check
    CHECK (level IN ('run_watch', 'exhaustion_watch', 'short_setup'));
