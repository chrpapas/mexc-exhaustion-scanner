"""Non-authorizing four-slot V40 admission plan over independently supplied evidence.

This module makes NO API calls, DB reads/writes, leases or exchange mutations.
It cannot issue a live permit. Live code must independently refresh inventory,
prove full durable ownership, acquire/fence the leader and enforce real margin,
book liquidity and risk immediately before any possible order.

This planner deliberately uses the FROZEN single-candle V40 admission policy,
not the prototype H4 entry-campaign VOL30 planner.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Sequence

from app.t100_live_account_reconciliation import assess_account_reconciliation
from app.t100_live_durable_recovery import DurableRecoveryReport
from app.t100_live_supervisor import ManagedShort
from app.t100_v40_admission import PriorBar, V40Admission, admission

MAX_SLOTS = 4


@dataclass(frozen=True)
class FourSlotPlan:
    action: str
    reason: str
    occupied_slots: int = 0
    modeled_notional_usdt: float = 0.0
    desired_notional_usdt: float = 0.0
    v40_admission: V40Admission | None = None
    candidate_symbol: str | None = None
    # These two flags are intentionally never made true by a planning result.
    entry_allowed: bool = False
    live_authorization: bool = False


def _count(value: Any) -> bool:
    return type(value) is int and value >= 0


def _verified_slot_symbols(
    inventory: Any,
    recovery: Any,
) -> tuple[tuple[str, ...], str]:
    """Require complete stable account plus real non-flat recovery result.

    A caller-supplied boolean, integer position count or manually labelled
    'managed' row is NOT accepted as T100 ownership evidence. The report is
    still a *read-only model input*, not a cryptographically trusted permit.
    """
    if not isinstance(inventory, dict):
        return (), "INVENTORY_EVIDENCE_MISSING"
    if (
        inventory.get("account_wide_completeness_verified") is not True
        or inventory.get("critical_inventory_equal_across_witness_window")
        is not True
    ):
        return (), "ACCOUNT_INVENTORY_NOT_CERTIFIED_STABLE"

    positions = inventory.get("after_positions")
    stops = inventory.get("after_current_tpsl")
    counts = inventory.get("open_order_counts")
    if not _count(positions) or not _count(stops) or not isinstance(counts, dict):
        return (), "INVENTORY_COUNTS_INVALID"

    names = (
        "sumCount", "limitOrderCount", "stopOrderCount",
        "planOrderCount", "trackOrderCount",
    )
    if any(not _count(counts.get(k)) for k in names):
        return (), "ORDER_COUNTS_INVALID"
    if counts["sumCount"] != sum(counts[k] for k in names[1:]):
        return (), "ORDER_COUNTS_INCOHERENT"
    if positions > MAX_SLOTS:
        return (), "ACCOUNT_EXCEEDS_FOUR_SLOTS"
    if stops != positions or counts["stopOrderCount"] != stops:
        return (), "POSITION_STOP_COUNT_MISMATCH"
    if any(counts[k] for k in ("limitOrderCount", "planOrderCount", "trackOrderCount")):
        return (), "UNRELATED_INFLIGHT_ORDERS_PRESENT"

    if positions == 0:
        if recovery is not None:
            return (), "FLAT_ACCOUNT_WITH_NONFLAT_RECOVERY_REPORT"
        account = assess_account_reconciliation(inventory)
        if account.get("flat_account_reconciled") is not True:
            return (), "FLAT_ACCOUNT_RECONCILIATION_FAILED"
        return (), "VERIFIED_FLAT_ACCOUNT"

    if not isinstance(recovery, DurableRecoveryReport):
        return (), "NONFLAT_DURABLE_RECOVERY_REQUIRED"
    if (
        recovery.mode != "SHADOW_ONLY_RECOVERED_NONFLAT"
        or recovery.live_authorization is not False
        or not isinstance(recovery.recovered, tuple)
        or len(recovery.recovered) != positions
        or any(type(x) is not ManagedShort for x in recovery.recovered)
        or any(type(getattr(recovery, x)) is not int for x in (
            "checkpoint_count", "exchange_position_count", "exchange_stop_count"
        ))
        or recovery.checkpoint_count != positions
        or recovery.exchange_position_count != positions
        or recovery.exchange_stop_count != stops
    ):
        return (), "NONFLAT_RECOVERY_REPORT_MISMATCH"

    symbols = tuple(p.symbol for p in recovery.recovered)
    positions_ids = tuple(p.position_id for p in recovery.recovered)
    stops_ids = tuple(p.stop_order_id for p in recovery.recovered)
    if (
        any(not isinstance(s, str) or not s for s in symbols)
        or any(not isinstance(x, str) or not x for x in positions_ids + stops_ids)
        or len(set(symbols)) != positions
        or len(set(positions_ids)) != positions
        or len(set(stops_ids)) != positions
    ):
        return (), "DUPLICATE_OR_INVALID_MANAGED_IDENTITY"

    account = assess_account_reconciliation(
        inventory, durable_nonflat_recovery_verified=True,
    )
    if account.get("managed_nonflat_account_reconciled") is not True:
        return (), "MANAGED_ACCOUNT_RECONCILIATION_FAILED"
    return symbols, "VERIFIED_MANAGED_ACCOUNT_MODEL"


def plan_four_slot_v40(
    *,
    inventory: Any,
    durable_recovery: Any,
    symbol: str,
    prior_bars: Sequence[PriorBar],
    admission_open_ts: int,
    stage1_atr7_pct: float,
    equity_usdt: float,
    marked_existing_usdt: float,
    used_same_bar_usdt: float = 0.0,
    cost_bps: float = 100.0,
) -> FourSlotPlan:
    """Model-only V40 decision. ADMIT is NEVER permission to submit an order.

    Caller must provide independent, fresh MEXC and durable evidence; this
    function does not independently validate snapshot age or serializability.
    """
    symbols, state = _verified_slot_symbols(inventory, durable_recovery)
    if state not in {"VERIFIED_FLAT_ACCOUNT", "VERIFIED_MANAGED_ACCOUNT_MODEL"}:
        return FourSlotPlan("BLOCK", state)
    occupied = len(symbols)
    if not isinstance(symbol, str) or re.fullmatch(r"[A-Z0-9]+_USDT", symbol) is None:
        return FourSlotPlan("BLOCK", "INVALID_CANDIDATE_SYMBOL", occupied)
    if symbol in {"BTC_USDT", "ETH_USDT"}:
        return FourSlotPlan("BLOCK", "EXCLUDED_CANDIDATE_SYMBOL", occupied)
    if occupied >= MAX_SLOTS:
        return FourSlotPlan("REJECT", "four_slot_limit", occupied)
    if symbol in symbols:
        return FourSlotPlan("REJECT", "duplicate_symbol", occupied)
    if type(admission_open_ts) is not int or type(prior_bars) not in (tuple, list):
        return FourSlotPlan("BLOCK", "INVALID_CANDIDATE_HISTORY", occupied)
    if any(type(bar) is not PriorBar for bar in prior_bars):
        return FourSlotPlan("BLOCK", "INVALID_CANDIDATE_HISTORY", occupied)

    try:
        modeled = admission(
            prior_bars=prior_bars,
            admission_open_ts=admission_open_ts,
            equity_usdt=equity_usdt,
            marked_existing_usdt=marked_existing_usdt,
            stage1_atr7_pct=stage1_atr7_pct,
            active_positions=occupied,
            already_open_symbol=False,
            used_same_bar_usdt=used_same_bar_usdt,
            cost_bps=cost_bps,
        )
    except (TypeError, ValueError, OverflowError):
        return FourSlotPlan("BLOCK", "INVALID_FROZEN_V40_INPUTS", occupied)

    return FourSlotPlan(
        action=modeled.action,
        reason=modeled.reason,
        occupied_slots=occupied,
        modeled_notional_usdt=modeled.modeled_notional_usdt,
        desired_notional_usdt=modeled.desired_notional_usdt,
        v40_admission=modeled,
        candidate_symbol=symbol,
    )
