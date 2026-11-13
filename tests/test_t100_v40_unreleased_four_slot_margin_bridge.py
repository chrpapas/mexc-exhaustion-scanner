"""Offline V40 four-slot/venue margin differences are never order permits."""
from dataclasses import replace
from decimal import Decimal as D

import pytest

from app.t100_v40_historical_margin_preview import V40HistoricalMarginPlan
from app.t100_v40_unreleased_four_slot_margin_bridge import (
    FourSlotMarginBridgeHalted, HistoricalShortSlot,
    project_four_slot_margin_feasibility,
)


def slot(i, n, *, leverage=1, target=None):
    v = D(str(n))
    p = V40HistoricalMarginPlan(
        status="V40_HISTORICAL_SIZE_EXCEEDS_PILOT_CAP",
        admission_action="ADMIT", admission_reason="modeled_single_bar_admission",
        tier="HIGH" if target is not None else "LOW",
        target_notional_usdt=target if target is not None else v,
        modeled_notional_usdt=v, lot_quantized_notional_usdt=v,
        volume_contracts=D(1), exchange_leverage_observed=leverage,
        historical_shock_leverage_proxy=5,
        nominal_exchange_initial_margin_usdt=v / D(leverage),
        current_pilot_cap_usdt=D("12.50"), historical_sizing_within_pilot_cap=v<=D("12.50"),
    )
    return HistoricalShortSlot(f"C{i}_USDT", p)


def project(slots, *, equity=D("2000"), collateral=D("2000"), leverage=1):
    return project_four_slot_margin_feasibility(
        slots=slots, equity_usdt=equity,
        available_cross_collateral_usdt=collateral,
        pilot_total_notional_ceiling_usdt=D("12.50"),
        exchange_leverage_observed=leverage,
    )


def test_four_high_unclipped_targets_fail_one_x_nominal_margin():
    r = project(tuple(slot(i,D(1250),target=D(1250)) for i in range(4)))
    assert r.portfolio_notional_usdt==D(5000)
    assert r.gross_exposure_pct_of_equity==D(250)
    assert r.nominal_initial_margin_usdt==D(5000)
    assert r.nominal_available_margin_shortfall_usdt==D(3000)
    assert r.simultaneous_sl75_price_loss_usdt==D(3750)
    assert r.equity_after_simultaneous_sl75_usdt==D(-1750)
    assert r.status=="V40_PORTFOLIO_NOMINAL_MARGIN_SHORTFALL_NONAUTHORIZING"
    assert not r.entry_allowed and not r.live_authorization and r.mexc_posts==0
    assert not r.maintenance_margin_verified and not r.liquidation_safety_certified


def test_five_x_can_fit_nominal_margin_but_not_joint_sl75_risk():
    r=project(tuple(slot(i,D(1250),leverage=5,target=D(1250)) for i in range(4)),leverage=5)
    assert r.nominal_initial_margin_usdt==D(1000)
    assert r.nominal_initial_margin_fits_available_collateral
    assert r.joint_sl75_loss_exceeds_equity
    assert r.status=="V40_PORTFOLIO_JOINT_SL75_STRESS_EXCEEDS_EQUITY_NONAUTHORIZING"
    assert not r.live_authorization


def test_four_low_positions_are_still_different_from_one_position_canary():
    r=project(tuple(slot(i,D(500)) for i in range(4)))
    assert r.portfolio_notional_usdt==D(2000)
    assert r.equity_after_simultaneous_sl75_usdt==D(500)
    assert r.status=="V40_PORTFOLIO_DIFFERENT_FROM_CURRENT_CANARY_NONAUTHORIZING"


def test_one_small_quantized_position_fits_pilot_but_does_not_authorize():
    r=project([slot(0,D("12.4"))],equity=D("56.37671495"),collateral=D("56.37671495"))
    assert r.matches_current_single_position_pilot_cap
    assert r.status=="V40_PORTFOLIO_PROJECTION_ONLY_NONAUTHORIZING"
    assert not r.live_authorization and not r.liquidation_safety_certified


@pytest.mark.parametrize("slots,equity,collateral,leverage,code",[
    ([],D(100),D(100),1,"SLOT_COUNT"),
    ([slot(0,1)]*5,D(100),D(100),1,"SLOT_COUNT"),
    ([slot(0,1),slot(0,1)],D(100),D(100),1,"DUPLICATE"),
    ([slot(0,1)],D("NaN"),D(100),1,"EQUITY"),
    ([slot(0,1)],D(100),D("-1"),1,"COLLATERAL"),
    ([slot(0,1)],D(100),D(100),True,"LEVERAGE"),
    ([slot(0,1)],D(100),D(100),5,"ADMISSION_OR_LEVERAGE"),
    ([HistoricalShortSlot("C0_USDT",replace(slot(0,1).plan,modeled_notional_usdt=D("0.5")))],D(100),D(100),1,"SIZE_PARITY"),
    ([HistoricalShortSlot("C0_USDT",replace(slot(0,1).plan,nominal_exchange_initial_margin_usdt=D("0.5")))],D(100),D(100),1,"MARGIN_PARITY"),
])
def test_fail_closed_invalid_inputs(slots,equity,collateral,leverage,code):
    with pytest.raises(FourSlotMarginBridgeHalted,match=code):
        project(slots,equity=equity,collateral=collateral,leverage=leverage)
