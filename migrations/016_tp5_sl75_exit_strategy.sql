-- v1.3.31: allow persisted TP5 + catastrophic SL75 positions.
-- The application introduced exit_strategy='tp5_sl75_full' in v1.3.24,
-- but migration 015's CHECK constraint still only allowed tp5_full.

ALTER TABLE trader_positions DROP CONSTRAINT IF EXISTS trader_positions_exit_strategy_check;
ALTER TABLE trader_positions ADD CONSTRAINT trader_positions_exit_strategy_check
    CHECK (exit_strategy IN (
        'ratchet_5','trailing_5','trailing_15_floor_20',
        'fixed_time_standard','tp20_or_timeout','tp5_full','tp5_sl75_full'
    ));
