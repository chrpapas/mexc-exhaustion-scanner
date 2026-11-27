"""Integration: frozen sequential multi-slot admission -> lot quantization -> margin."""
from dataclasses import replace
from decimal import Decimal as D

import pytest

from app.t100_v40_unreleased_full_portfolio import project_frozen_v40_multi_slot
from app.t100_v40_unreleased_four_slot_margin_bridge import FourSlotMarginBridgeHalted
from app.t100_v40_unreleased_sequential_lot_margin import (
    V40ObservedContractLot, project_frozen_sequential_lots_and_margin,
)
from tests.test_t100_v40_unreleased_full_portfolio import batch


def projection(*, atr=5.8, owned=()):
    return project_frozen_v40_multi_slot(
        source=batch(5,atr=atr),equity_usdt=2000.,
        marked_existing_usdt=0.,owned_open_symbols=owned,
    )


def specs(proj, *, max_volume="1000000"):
    return {e.symbol:V40ObservedContractLot(
        entry_price=D("100"),contract_size="0.001",
        volume_step="1",min_volume="1",max_volume=max_volume,
    ) for e in proj.entries}


def audit(proj, *, leverage=1, metadata=None):
    return project_frozen_sequential_lots_and_margin(
        historical=proj,contracts=metadata if metadata is not None else specs(proj),
        equity_usdt=D(2000),available_cross_collateral_usdt=D(2000),
        pilot_total_notional_ceiling_usdt=D("12.50"),
        exchange_leverage_observed=leverage,
    )


def test_sequential_four_high_shock_clips_below_naive_250pct():
    proj=projection()
    assert len(proj.entries)==4
    assert proj.entries[0].desired_notional_usdt==1250.
    assert proj.entries[3].modeled_notional_usdt < 1250.
    r=audit(proj)
    assert D(200) < r.margin.gross_exposure_pct_of_equity < D(250)
    assert r.margin.nominal_initial_margin_fits_available_collateral is False
    assert r.margin.joint_sl75_loss_exceeds_equity is True
    assert r.hypothetical_child_orders==4
    assert not r.live_authorization and not r.margin.live_authorization
    assert not r.exchange_contract_specs_authenticated and r.mexc_posts==0


def test_five_x_applies_only_to_nominal_margin_not_stop_loss():
    r=audit(projection(),leverage=5)
    assert r.margin.nominal_initial_margin_fits_available_collateral
    assert r.margin.joint_sl75_loss_exceeds_equity
    assert not r.margin.liquidation_safety_certified


def test_existing_ledger_positions_require_complete_reconstruction():
    proj=projection(owned=("ALT1_USDT",))
    assert proj.resulting_slot_count > len(proj.entries)
    with pytest.raises(FourSlotMarginBridgeHalted,match="FROM_FLAT"):
        audit(proj)


def test_missing_contract_and_unfillable_lot_halt():
    proj=projection()
    missing=specs(proj)
    missing.pop(proj.entries[0].symbol)
    with pytest.raises(FourSlotMarginBridgeHalted,match="SYMBOL_COVERAGE"):
        audit(proj,metadata=missing)
    tiny={k:replace(v,min_volume="50000") for k,v in specs(proj).items()}
    with pytest.raises(FourSlotMarginBridgeHalted,match="UNFILLABLE_CONTRACT_LOT"):
        audit(proj,metadata=tiny)


def test_tampered_portfolio_total_and_zero_leverage_halt():
    proj=projection()
    with pytest.raises(FourSlotMarginBridgeHalted,match="TOTAL_DRIFT"):
        audit(replace(proj,total_modeled_new_notional_usdt=0.))
    with pytest.raises(FourSlotMarginBridgeHalted,match="ORIGIN_UNVERIFIED"):
        audit(proj,leverage=0)
