"""Offline bridge: frozen sequential V40 four-slot admission -> V39 lot-floor -> margin.

A portfolio projection only contains *new* entries. Therefore this bridge
requires a complete from-flat admission projection; otherwise existing exposure
would be omitted from margin/stress accounting. No live risk permit is possible.
"""
from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal as D
from math import isfinite
from typing import Mapping

from app.t100_v40_admission import lot_quantize_v39
from app.t100_v40_historical_margin_preview import V40HistoricalMarginPlan
from app.t100_v40_unreleased_full_portfolio import (
    V40FullPortfolioProjection, V40ReferenceEntry,
)
from app.t100_v40_unreleased_four_slot_margin_bridge import (
    FourSlotMarginBridgeHalted, FourSlotMarginProjection,
    HistoricalShortSlot, project_four_slot_margin_feasibility,
)


@dataclass(frozen=True)
class V40ObservedContractLot:
    entry_price: D
    contract_size: str
    volume_step: str
    min_volume: str
    max_volume: str | None


@dataclass(frozen=True)
class V40SequentialLotMarginProjection:
    status: str
    margin: FourSlotMarginProjection
    hypothetical_child_orders: int
    all_lots_quantized: bool = True
    exchange_contract_specs_authenticated: bool = False
    historical_full_exit_replay_certified: bool = False
    live_authorization: bool = False
    mexc_posts: int = 0
    database_writes: int = 0


def project_frozen_sequential_lots_and_margin(
    *,
    historical: V40FullPortfolioProjection,
    contracts: Mapping[str,V40ObservedContractLot],
    equity_usdt: D,
    available_cross_collateral_usdt: D,
    pilot_total_notional_ceiling_usdt: D,
    exchange_leverage_observed: int,
) -> V40SequentialLotMarginProjection:
    """Use already sequentially clipped V40 admissions, not four independent caps.

    'ObservedContractLot' values are supplied, NOT authenticated by this
    offline module. A floor to zero aborts; do not assume the historical
    HYBRID replay's missing-spec passthrough is exchange executable.
    """
    if (
        type(historical) is not V40FullPortfolioProjection
        or historical.status != "V40_FROZEN_FOUR_SLOT_HISTORICAL_REFERENCE_ONLY"
        or historical.historical_reference_only is not True
        or historical.native_exchange_execution_verified is not False
        or historical.risk_leverage_approved is not False
        or historical.exchange_posts != 0
        or historical.live_authorization is not False
        or type(historical.entries) is not tuple
        or not 1 <= len(historical.entries) <= 4
        or type(historical.resulting_slot_count) is not int
        or historical.resulting_slot_count != len(historical.entries)
        or type(contracts) is not dict
        or type(equity_usdt) is not D
        or not equity_usdt.is_finite() or equity_usdt <= 0
        or type(available_cross_collateral_usdt) is not D
        or not available_cross_collateral_usdt.is_finite()
        or available_cross_collateral_usdt < 0
        or type(pilot_total_notional_ceiling_usdt) is not D
        or not pilot_total_notional_ceiling_usdt.is_finite()
        or pilot_total_notional_ceiling_usdt <= 0
        or type(exchange_leverage_observed) is not int
        or not 1 <= exchange_leverage_observed <= 125
    ):
        raise FourSlotMarginBridgeHalted("V40_SEQUENTIAL_COMPLETE_FROM_FLAT_ORIGIN_UNVERIFIED")
    if any(type(e) is not V40ReferenceEntry or type(e.symbol) is not str
           for e in historical.entries):
        raise FourSlotMarginBridgeHalted("V40_SEQUENTIAL_ENTRY_MODEL_INVALID")
    symbols = [e.symbol for e in historical.entries]
    if len(set(symbols)) != len(symbols) or set(symbols) != set(contracts):
        raise FourSlotMarginBridgeHalted("V40_SEQUENTIAL_CONTRACT_SYMBOL_COVERAGE_INVALID")
    modeled = 0.
    slots = []
    children = 0
    for e in historical.entries:
        if (
            type(e) is not V40ReferenceEntry
            or type(e.symbol) is not str
            or not e.symbol.endswith("_USDT")
            or type(e.signal_id) is not int or e.signal_id <= 0
            or e.tier not in ("LOW_100", "HIGH_225")
            or type(e.desired_notional_usdt) is not float
            or type(e.modeled_notional_usdt) is not float
            or not isfinite(e.desired_notional_usdt)
            or not isfinite(e.modeled_notional_usdt)
            or e.desired_notional_usdt <= 0
            or e.modeled_notional_usdt <= 0
            or e.modeled_notional_usdt > e.desired_notional_usdt + 1e-8
        ):
            raise FourSlotMarginBridgeHalted("V40_SEQUENTIAL_ENTRY_MODEL_INVALID")
        target = equity_usdt * (D("0.625") if e.tier == "HIGH_225" else D("0.25"))
        if abs(D(str(e.desired_notional_usdt)) - target) > D("0.000001"):
            raise FourSlotMarginBridgeHalted("V40_SEQUENTIAL_TARGET_EQUITY_DRIFT")
        spec = contracts[e.symbol]
        if (
            type(spec) is not V40ObservedContractLot
            or type(spec.entry_price) is not D
            or not spec.entry_price.is_finite() or spec.entry_price <= 0
            or any(type(x) is not str for x in
                   (spec.contract_size,spec.volume_step,spec.min_volume))
            or (spec.max_volume is not None and type(spec.max_volume) is not str)
        ):
            raise FourSlotMarginBridgeHalted("V40_SEQUENTIAL_CONTRACT_SPEC_INVALID")
        try:
            qty,actual,nchildren = lot_quantize_v39(
                modeled_notional=e.modeled_notional_usdt,
                entry_price=float(spec.entry_price),
                contract_size=spec.contract_size,
                volume_step=spec.volume_step,
                min_volume=spec.min_volume,
                max_volume=spec.max_volume,
            )
        except Exception:
            raise FourSlotMarginBridgeHalted("V40_SEQUENTIAL_LOT_FLOOR_INVALID") from None
        if qty <= 0 or actual <= 0 or nchildren <= 0:
            raise FourSlotMarginBridgeHalted("V40_SEQUENTIAL_UNFILLABLE_CONTRACT_LOT")
        if actual > D(str(e.modeled_notional_usdt)) + D("0.0000001"):
            raise FourSlotMarginBridgeHalted("V40_SEQUENTIAL_QUANTIZED_SIZE_EXCEEDS_MODEL")
        # Build an explicitly nonauthorizing model plan from the sequential
        # decisions, not from four independent calls that reset marked margin.
        plan = V40HistoricalMarginPlan(
            status="V40_HISTORICAL_SEQUENTIAL_LOT_ONLY_NONAUTHORIZING",
            admission_action="ADMIT", admission_reason="frozen_sequential_model",
            tier="HIGH" if e.tier == "HIGH_225" else "LOW",
            target_notional_usdt=target,
            modeled_notional_usdt=D(str(e.modeled_notional_usdt)),
            lot_quantized_notional_usdt=actual,
            volume_contracts=qty,
            exchange_leverage_observed=exchange_leverage_observed,
            historical_shock_leverage_proxy=5,
            nominal_exchange_initial_margin_usdt=actual/D(exchange_leverage_observed),
            current_pilot_cap_usdt=pilot_total_notional_ceiling_usdt,
            historical_sizing_within_pilot_cap=actual <= pilot_total_notional_ceiling_usdt,
        )
        slots.append(HistoricalShortSlot(e.symbol, plan))
        modeled += e.modeled_notional_usdt
        children += nchildren
    if (
        type(historical.total_modeled_new_notional_usdt) is not float
        or not isfinite(historical.total_modeled_new_notional_usdt)
        or abs(modeled-historical.total_modeled_new_notional_usdt)>1e-7
    ):
        raise FourSlotMarginBridgeHalted("V40_SEQUENTIAL_PORTFOLIO_TOTAL_DRIFT")
    margin = project_four_slot_margin_feasibility(
        slots=tuple(slots),equity_usdt=equity_usdt,
        available_cross_collateral_usdt=available_cross_collateral_usdt,
        pilot_total_notional_ceiling_usdt=pilot_total_notional_ceiling_usdt,
        exchange_leverage_observed=exchange_leverage_observed,
    )
    return V40SequentialLotMarginProjection(
        status="V40_SEQUENTIAL_LOT_AND_MARGIN_OFFLINE_NONAUTHORIZING",
        margin=margin,hypothetical_child_orders=children,
    )
