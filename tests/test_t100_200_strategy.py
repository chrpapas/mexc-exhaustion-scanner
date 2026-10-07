from app.t100_200_strategy import (
    HIGH_TIER,
    LOW_TIER,
    Stage1Gate,
    evaluate_completed_bar,
    notional_fraction,
    stop_price,
)


def test_stage1_gate_and_tier_boundaries():
    assert not Stage1Gate(False, 0.20, 6.0).eligible
    assert not Stage1Gate(True, 0.149999, 6.0).eligible
    assert not Stage1Gate(True, 0.20, 3.9999).eligible
    low = Stage1Gate(True, 0.15, 4.0)
    assert low.eligible and low.tier == LOW_TIER
    assert Stage1Gate(True, 0.15, 5.6999).tier == LOW_TIER
    assert Stage1Gate(True, 0.15, 5.7).tier == HIGH_TIER


def test_t100_200_notional_fractions_are_per_current_equity():
    assert notional_fraction(LOW_TIER) == 0.25
    assert notional_fraction(HIGH_TIER) == 0.50


def test_pretrail_sl75_is_adverse_first():
    d = evaluate_completed_bar(
        entry=100.0,
        high=180.0,
        low=80.0,
        trail_active=False,
        best_profit_pct=0.0,
    )
    assert d.reason == "sl75"
    assert d.exit_price == stop_price(100.0) == 175.0


def test_new_trail_activation_cannot_exit_same_bar():
    d = evaluate_completed_bar(
        entry=100.0,
        high=100.0,
        low=89.0,
        trail_active=False,
        best_profit_pct=0.0,
    )
    assert d.exit_price is None
    assert d.trail_active
    assert d.best_profit_pct == 11.0


def test_existing_trail_uses_best_minus_one_percentage_point():
    d = evaluate_completed_bar(
        entry=100.0,
        high=92.5,
        low=80.0,
        trail_active=True,
        best_profit_pct=10.0,
    )
    assert d.reason == "trail_gap1"
    assert d.exit_price == 91.0
