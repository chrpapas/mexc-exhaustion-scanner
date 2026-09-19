-- v1.3.75: production candidate TP5 no-stop recovery runner.
-- New positions may take 50% at TP5 after >=30% prior adverse excursion and
-- keep the remaining 50% behind a 1 percentage-point profit-return trail.
-- Existing persisted positions retain their original exit_strategy.

ALTER TABLE trader_positions DROP CONSTRAINT IF EXISTS trader_positions_exit_strategy_check;
ALTER TABLE trader_positions ADD CONSTRAINT trader_positions_exit_strategy_check
    CHECK (exit_strategy IN (
        'ratchet_5','trailing_5','trailing_15_floor_20',
        'fixed_time_standard','tp20_or_timeout','tp5_full','tp5_sl75_full',
        'tp5_adv30_runner50_trail1'
    ));
