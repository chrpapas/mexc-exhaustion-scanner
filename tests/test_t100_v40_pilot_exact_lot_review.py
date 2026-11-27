"""Offline V40 pilot sizing: no fake trade, MEXC POST or SQL mutation."""
from decimal import Decimal as D

import pytest

from app.t100_v40_pilot_exact_lot_review import review_v40_pilot_exact_lot


def review(**changes):
    config=dict(
        signal_id=496,symbol="IDOL_USDT",reference_price="100",
        frozen_modeled_notional_usdt="35.23544684",
        equity_usdt="56.37671495",available_usdt="56.37671495",
        contract_size="0.1",volume_step="1",min_volume="1",
        max_volume="100",price_tick=".01",max_order_usdt="12.50",
        max_equity_fraction=".25",max_daily_loss_usdt="12.50",
        realized_today_loss_usdt="0",max_slippage_bps="100",
    )
    config.update(changes)
    return review_v40_pilot_exact_lot(**config)


def test_high_225_model_is_capped_and_rounded_down_not_up_to_grid():
    out=review()
    assert out.status=="V40_PILOT_LOT_FEASIBLE_NONAUTHORIZING"
    assert out.capped_target_usdt==D("12.50")
    assert out.executable_notional_usdt==D("10.0")
    assert out.executable_contracts==D(1)
    assert D("7.5") < out.modeled_stop_loss_usdt < D("8")
    assert out.exact_depth_verified is False
    assert out.exchange_market_stop_verified is False
    assert out.entry_allowed is False and out.live_authorization is False
    assert out.database_writes==0 and out.mexc_orders_sent==0


def test_low_100_model_keeps_frozen_smaller_model_before_pilot_ceiling():
    out=review(frozen_modeled_notional_usdt="7.31",contract_size=".01")
    assert out.status=="V40_PILOT_LOT_FEASIBLE_NONAUTHORIZING"
    assert out.capped_target_usdt==D("7.31")
    assert out.executable_notional_usdt==D("7.00")
    assert out.executable_contracts==D(7)


def test_small_account_25_percent_cap_applies_before_rounding():
    out=review(equity_usdt="20",available_usdt="20",contract_size=".01")
    assert out.capped_target_usdt==D("5")
    assert out.executable_notional_usdt==D("5.00")


def test_zero_lots_do_not_round_up_to_exchange_minimum():
    out=review(contract_size="1",volume_step="1",min_volume="1")
    assert out.status=="V40_PILOT_LOT_EXCHANGE_MINIMUM_OR_GRID_REJECTED"
    assert out.executable_contracts==D(0)
    assert out.entry_allowed is False


def test_modeled_sl75_plus_friction_must_fit_daily_loss_budget():
    out=review(max_daily_loss_usdt="2")
    assert out.status=="V40_PILOT_LOT_MODELED_STOP_LOSS_EXCEEDS_DAILY_LIMIT"
    assert out.executable_notional_usdt==D("10")
    assert out.entry_allowed is False


@pytest.mark.parametrize("bad",[
    {"signal_id":True},{"symbol":"BTC_USDT"},{"equity_usdt":"NaN"},
    {"reference_price":"Infinity"},{"contract_size":"0"},
    {"frozen_modeled_notional_usdt":"-1"},
    {"max_equity_fraction":".5"},{"max_order_usdt":"25"},
    {"available_usdt":"100"},{"realized_today_loss_usdt":"12.50"},
    {"max_slippage_bps":"301"},
])
def test_fail_closed_on_invalid_or_excessive_pilot_budget(bad):
    out=review(**bad)
    assert out.status in (
        "V40_PILOT_LOT_INPUTS_INVALID",
        "V40_PILOT_LOT_RISK_BOUNDARY_INVALID",
    )
    assert out.executable_notional_usdt==D(0)
    assert out.entry_allowed is False


def test_exact_roundoff_cannot_invent_extra_spendable_margin():
    out=review(
        equity_usdt="56.37671495",
        available_usdt="56.376714959",
    )
    assert out.capped_target_usdt==D("12.50")
    assert out.executable_notional_usdt==D("10")
