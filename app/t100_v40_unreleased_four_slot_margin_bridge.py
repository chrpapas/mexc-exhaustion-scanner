"""Offline, nonauthorizing V40 historical four-slot vs exchange-margin projection.

This module tests mathematical feasibility of proposed, precomputed and
lot-quantized V40 admission plans. It does NOT reconstruct the historical
portfolio replay, authenticate exchange balances or maintenance tiers, predict
liquidation prices, or authorize an order. All inputs must be independently
proven before any live use (which is deliberately impossible here).
"""
from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal as D, InvalidOperation
from typing import Sequence

from app.t100_v40_historical_margin_preview import V40HistoricalMarginPlan

MAX_SLOTS = 4
SL75_ADVERSE_PRICE_FRACTION = D("0.75")


class FourSlotMarginBridgeHalted(ValueError):
    pass


@dataclass(frozen=True)
class HistoricalShortSlot:
    symbol: str
    plan: V40HistoricalMarginPlan


@dataclass(frozen=True)
class FourSlotMarginProjection:
    status: str
    slot_count: int
    portfolio_notional_usdt: D
    portfolio_target_before_clipping_usdt: D
    gross_exposure_pct_of_equity: D
    nominal_initial_margin_usdt: D
    nominal_available_margin_shortfall_usdt: D
    simultaneous_sl75_price_loss_usdt: D
    equity_after_simultaneous_sl75_usdt: D
    nominal_initial_margin_fits_available_collateral: bool
    joint_sl75_loss_exceeds_equity: bool
    matches_current_single_position_pilot_cap: bool
    maintenance_margin_verified: bool = False
    liquidation_safety_certified: bool = False
    live_authorization: bool = False
    entry_allowed: bool = False
    mexc_posts: int = 0
    database_writes: int = 0


def _finite_money(value: object, code: str, *, positive: bool) -> D:
    if type(value) is not D:
        raise FourSlotMarginBridgeHalted(code)
    try:
        valid = value.is_finite() and (value > 0 if positive else value >= 0)
    except (InvalidOperation, ValueError):
        valid = False
    if not valid:
        raise FourSlotMarginBridgeHalted(code)
    return value


def project_four_slot_margin_feasibility(
    *,
    slots: Sequence[HistoricalShortSlot],
    equity_usdt: D,
    available_cross_collateral_usdt: D,
    pilot_total_notional_ceiling_usdt: D,
    exchange_leverage_observed: int,
) -> FourSlotMarginProjection:
    """Reconcile quantized V40 notionals with nominal exchange-margin arithmetic.

    A +75% adverse move is a joint mathematical stress scenario, NOT a
    liquidation estimate or a claim that the historical replay had such a
    simultaneous move. It excludes fees, funding, maintenance requirements,
    gap risk, and all exchange mechanics. Never infer live safety from PASS.
    """
    equity = _finite_money(equity_usdt, "V40_PORTFOLIO_EQUITY_INVALID", positive=True)
    collateral = _finite_money(
        available_cross_collateral_usdt, "V40_PORTFOLIO_COLLATERAL_INVALID", positive=False
    )
    pilot_cap = _finite_money(
        pilot_total_notional_ceiling_usdt, "V40_PORTFOLIO_PILOT_CEILING_INVALID", positive=True
    )
    if type(exchange_leverage_observed) is not int or not 1 <= exchange_leverage_observed <= 125:
        raise FourSlotMarginBridgeHalted("V40_PORTFOLIO_LEVERAGE_UNVERIFIED")
    if type(slots) not in (tuple, list) or not 1 <= len(slots) <= MAX_SLOTS:
        raise FourSlotMarginBridgeHalted("V40_PORTFOLIO_SLOT_COUNT_INVALID")
    names: set[str] = set()
    target = D(0)
    actual = D(0)
    for slot in slots:
        if type(slot) is not HistoricalShortSlot or not isinstance(slot.plan, V40HistoricalMarginPlan):
            raise FourSlotMarginBridgeHalted("V40_PORTFOLIO_SLOT_SHAPE_INVALID")
        if (type(slot.symbol) is not str or not slot.symbol or slot.symbol != slot.symbol.strip()
                or slot.symbol in names):
            raise FourSlotMarginBridgeHalted("V40_PORTFOLIO_DUPLICATE_OR_INVALID_SYMBOL")
        names.add(slot.symbol)
        p = slot.plan
        if (p.admission_action != "ADMIT" or p.tier not in ("LOW", "HIGH")
                or type(p.exchange_leverage_observed) is not int
                or p.exchange_leverage_observed != exchange_leverage_observed
                or p.live_authorization is not False or p.live_order_allowed is not False
                or p.exchange_posts != 0 or p.exchange_config_mutations != 0
                or p.historical_shock_leverage_proxy != 5):
            raise FourSlotMarginBridgeHalted("V40_PORTFOLIO_UNVERIFIED_ADMISSION_OR_LEVERAGE")
        desired = _finite_money(p.target_notional_usdt, "V40_PORTFOLIO_TARGET_INVALID", positive=True)
        modeled = _finite_money(p.modeled_notional_usdt, "V40_PORTFOLIO_MODELED_INVALID", positive=True)
        quantized = _finite_money(
            p.lot_quantized_notional_usdt, "V40_PORTFOLIO_QUANTIZED_INVALID", positive=True
        )
        original_nominal_margin = _finite_money(
            p.nominal_exchange_initial_margin_usdt,
            "V40_PORTFOLIO_PLAN_NOMINAL_MARGIN_INVALID", positive=True
        )
        if original_nominal_margin != quantized / D(exchange_leverage_observed):
            raise FourSlotMarginBridgeHalted("V40_PORTFOLIO_PLAN_MARGIN_PARITY_DRIFT")
        if modeled > desired + D("0.0000001") or quantized > modeled + D("0.0000001"):
            raise FourSlotMarginBridgeHalted("V40_PORTFOLIO_SIZE_PARITY_DRIFT")
        target += desired
        actual += quantized
    margin = actual / D(exchange_leverage_observed)
    stress_loss = actual * SL75_ADVERSE_PRICE_FRACTION
    within_canary = len(slots) == 1 and actual <= pilot_cap
    margin_fits = margin <= collateral
    joint_loss_exceeds = stress_loss > equity
    status = (
        "V40_PORTFOLIO_NOMINAL_MARGIN_SHORTFALL_NONAUTHORIZING" if not margin_fits else
        "V40_PORTFOLIO_JOINT_SL75_STRESS_EXCEEDS_EQUITY_NONAUTHORIZING" if joint_loss_exceeds else
        "V40_PORTFOLIO_DIFFERENT_FROM_CURRENT_CANARY_NONAUTHORIZING" if not within_canary else
        "V40_PORTFOLIO_PROJECTION_ONLY_NONAUTHORIZING"
    )
    return FourSlotMarginProjection(
        status=status, slot_count=len(slots), portfolio_notional_usdt=actual,
        portfolio_target_before_clipping_usdt=target,
        gross_exposure_pct_of_equity=actual / equity * D(100),
        nominal_initial_margin_usdt=margin,
        nominal_available_margin_shortfall_usdt=max(D(0), margin - collateral),
        simultaneous_sl75_price_loss_usdt=stress_loss,
        equity_after_simultaneous_sl75_usdt=equity - stress_loss,
        nominal_initial_margin_fits_available_collateral=margin_fits,
        joint_sl75_loss_exceeds_equity=joint_loss_exceeds,
        matches_current_single_position_pilot_cap=within_canary,
    )
