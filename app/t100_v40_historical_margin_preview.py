"""Frozen V40 historical margin/lot sizing preview; NOT exchange authorization.

Reconstruct the original V40 one-candle desired size, shock budget, causal
VOL30/H4 constraints and V39 contract-lot floor, then compare against actual
observed pilot notional caps and nominal exchange margin. The 5x pooled
shock model is historical research, NOT a certified MEXC leverage setting.

This projection never changes leverage, margin type, stops, positions or
trader controls, and cannot be used to submit a live order.
"""
from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal as D, InvalidOperation
from typing import Sequence

from app.t100_v40_admission import PriorBar, admission, lot_quantize_v39

HISTORICAL_SHOCK_LEVERAGE_PROXY = 5
HISTORICAL_MAX_POSITIONS = 4


class V40HistoricalMarginPlanHalted(ValueError):
    pass


@dataclass(frozen=True)
class V40HistoricalMarginPlan:
    status: str
    admission_action: str
    admission_reason: str
    tier: str
    target_notional_usdt: D
    modeled_notional_usdt: D
    lot_quantized_notional_usdt: D
    volume_contracts: D
    exchange_leverage_observed: int
    historical_shock_leverage_proxy: int
    nominal_exchange_initial_margin_usdt: D
    current_pilot_cap_usdt: D
    historical_sizing_within_pilot_cap: bool
    exchange_posts: int = 0
    exchange_config_mutations: int = 0
    live_order_allowed: bool = False
    live_authorization: bool = False


def _num(value: object, name: str, allow_zero: bool = False) -> D:
    try:
        val = D(str(value))
    except (InvalidOperation, TypeError, ValueError):
        raise V40HistoricalMarginPlanHalted("V40_HISTORICAL_"+name+"_INVALID") from None
    if not val.is_finite() or (val < 0 if allow_zero else val <= 0):
        raise V40HistoricalMarginPlanHalted("V40_HISTORICAL_"+name+"_INVALID")
    return val


def compare_historical_v40_lot_and_margin(
    *,
    prior_bars: Sequence[PriorBar],
    admission_open_ts: int,
    equity_usdt: D,
    marked_existing_usdt: D,
    stage1_atr7_pct: float,
    active_positions: int,
    already_open_symbol: bool,
    used_same_bar_usdt: D,
    entry_price: D,
    contract_size: str,
    volume_step: str,
    min_volume: str,
    max_volume: str | None,
    exchange_leverage_observed: int,
    pilot_notional_cap_usdt: D,
    cost_bps: float = 100.,
) -> V40HistoricalMarginPlan:
    """Conservative model-to-venue *comparison*, not a mutable risk permit."""
    equity = _num(equity_usdt, "EQUITY")
    marked = _num(marked_existing_usdt, "MARKED", True)
    same_bar = _num(used_same_bar_usdt, "SAME_BAR", True)
    price = _num(entry_price, "ENTRY_PRICE")
    pilot_cap = _num(pilot_notional_cap_usdt, "CAP")
    if (
        type(equity_usdt) is not D
        or type(marked_existing_usdt) is not D
        or type(used_same_bar_usdt) is not D
        or type(entry_price) is not D
        or type(pilot_notional_cap_usdt) is not D
        or type(exchange_leverage_observed) is not int
        or not 1 <= exchange_leverage_observed <= 125
        or type(active_positions) is not int
        or not 0 <= active_positions <= HISTORICAL_MAX_POSITIONS
        or type(already_open_symbol) is not bool
        or type(admission_open_ts) is not int
    ):
        raise V40HistoricalMarginPlanHalted("V40_HISTORICAL_ACCOUNT_SCOPE_INVALID")
    try:
        result = admission(
            prior_bars=prior_bars,
            admission_open_ts=admission_open_ts,
            equity_usdt=float(equity),
            marked_existing_usdt=float(marked),
            stage1_atr7_pct=stage1_atr7_pct,
            active_positions=active_positions,
            already_open_symbol=already_open_symbol,
            used_same_bar_usdt=float(same_bar),
            cost_bps=cost_bps,
        )
    except Exception:
        raise V40HistoricalMarginPlanHalted("V40_HISTORICAL_ADMISSION_UNVERIFIED") from None
    tier = "HIGH" if stage1_atr7_pct >= 5.7 else "LOW"
    # Report the exact equity fraction; original admission uses binary floats.
    # Require float-modeled sizing to agree within sub-cent arithmetic noise.
    target = equity * (D("0.625") if tier == "HIGH" else D("0.25"))
    if abs(target - D(str(result.desired_notional_usdt))) > D("0.000000001"):
        raise V40HistoricalMarginPlanHalted("V40_HISTORICAL_TARGET_PARITY_DRIFT")
    modeled = D(str(result.modeled_notional_usdt))
    qty = D("0")
    actual = D("0")
    if result.action == "ADMIT":
        try:
            qty,actual,_ = lot_quantize_v39(
                modeled_notional=result.modeled_notional_usdt,
                entry_price=float(price),
                contract_size=contract_size,
                volume_step=volume_step,
                min_volume=min_volume,
                max_volume=max_volume,
            )
        except Exception:
            raise V40HistoricalMarginPlanHalted("V40_HISTORICAL_LOT_METADATA_INVALID") from None
    if actual > modeled + D("0.0000001"):
        raise V40HistoricalMarginPlanHalted("V40_HISTORICAL_QUANTIZED_SIZE_EXCEEDS_MODEL")
    within = bool(result.action == "ADMIT" and actual > 0 and actual <= pilot_cap)
    status = (
        "V40_HISTORICAL_SIGNAL_NOT_ADMITTED"
        if result.action != "ADMIT"
        else "V40_HISTORICAL_NO_TRADABLE_CONTRACT_LOT"
        if actual <= 0
        else "V40_HISTORICAL_SIZE_EXCEEDS_PILOT_CAP"
        if not within
        else "V40_HISTORICAL_SIZE_WITHIN_PILOT_CAP_NONAUTHORIZING"
    )
    return V40HistoricalMarginPlan(
        status=status,
        admission_action=result.action,
        admission_reason=result.reason,
        tier=tier,
        target_notional_usdt=target,
        modeled_notional_usdt=modeled,
        lot_quantized_notional_usdt=actual,
        volume_contracts=qty,
        exchange_leverage_observed=exchange_leverage_observed,
        historical_shock_leverage_proxy=HISTORICAL_SHOCK_LEVERAGE_PROXY,
        nominal_exchange_initial_margin_usdt=(
            actual / D(exchange_leverage_observed)
        ),
        current_pilot_cap_usdt=pilot_cap,
        historical_sizing_within_pilot_cap=within,
    )
