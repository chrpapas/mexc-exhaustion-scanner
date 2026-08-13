from app.trader_logic import maturity_exit_reason, next_profit_floor, short_return_pct


def test_short_return_pct():
    assert short_return_pct(100.0, 80.0) == 20.0
    assert short_return_pct(100.0, 200.0) == -100.0
    assert short_return_pct(100.0, 500.0) == -400.0


def test_ratchet_floor_arms_at_20_and_moves_every_5():
    kwargs = dict(
        exit_strategy="ratchet_5",
        activation_pct=20.0,
        step_pct=5.0,
        giveback_pct=5.0,
    )
    assert next_profit_floor(peak_profit_pct=19.99, **kwargs) is None
    assert next_profit_floor(peak_profit_pct=20.0, **kwargs) == 20.0
    assert next_profit_floor(peak_profit_pct=24.99, **kwargs) == 20.0
    assert next_profit_floor(peak_profit_pct=25.0, **kwargs) == 25.0
    assert next_profit_floor(peak_profit_pct=39.9, **kwargs) == 35.0
    assert next_profit_floor(peak_profit_pct=40.0, **kwargs) == 40.0


def test_trailing_floor_never_drops_below_20():
    kwargs = dict(
        exit_strategy="trailing_5",
        activation_pct=20.0,
        step_pct=5.0,
        giveback_pct=5.0,
    )
    assert next_profit_floor(peak_profit_pct=20.0, **kwargs) == 20.0
    assert next_profit_floor(peak_profit_pct=24.0, **kwargs) == 20.0
    assert next_profit_floor(peak_profit_pct=30.0, **kwargs) == 25.0
    assert next_profit_floor(peak_profit_pct=47.5, **kwargs) == 42.5

def test_profit_20_maturity_closes_only_at_target():
    assert maturity_exit_reason(
        position_maturity="profit_20", current_return_pct=19.99, age_seconds=999999, profit_target_pct=20.0
    ) is None
    assert maturity_exit_reason(
        position_maturity="profit_20", current_return_pct=20.01, age_seconds=5, profit_target_pct=20.0
    ) == "profit_target_20"


def test_fixed_maturity_closes_at_horizon_regardless_of_return():
    assert maturity_exit_reason(
        position_maturity="3d", current_return_pct=-40.0, age_seconds=259199, profit_target_pct=20.0
    ) is None
    assert maturity_exit_reason(
        position_maturity="3d", current_return_pct=-40.0, age_seconds=259200, profit_target_pct=20.0
    ) == "maturity_3d"

