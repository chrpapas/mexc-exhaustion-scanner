-- v1.3.0: persist tier-specific Standard fixed-time and High-Risk TP-or-timeout strategies.

ALTER TABLE trader_positions DROP CONSTRAINT IF EXISTS trader_positions_exit_strategy_check;
ALTER TABLE trader_positions ADD CONSTRAINT trader_positions_exit_strategy_check
    CHECK (exit_strategy IN (
        'ratchet_5','trailing_5','trailing_15_floor_20',
        'fixed_time_standard','tp20_or_timeout'
    ));

ALTER TABLE trader_positions DROP CONSTRAINT IF EXISTS trader_positions_position_maturity_check;
ALTER TABLE trader_positions ADD CONSTRAINT trader_positions_position_maturity_check
    CHECK (position_maturity IN (
        'profit_20','1d','2d','3d','4d','5d','6d','7d','10d','14d'
    ));
