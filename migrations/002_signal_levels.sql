ALTER TABLE run_signals DROP CONSTRAINT IF EXISTS run_signals_level_check;
ALTER TABLE run_signals
    ADD CONSTRAINT run_signals_level_check
    CHECK (level IN ('watch', 'candidate', 'short_setup'));
