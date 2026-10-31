"""Pure fail-closed SHARED cross-margin pilot observation (no authorization).

Existing operator positions are never adopted as T100-owned positions. They
remain in the same USDT cross-margin collateral and affect all-account risk.
This module is a narrow inventory/manual-risk/nominal-loss precheck ONLY; it cannot
certify liquidation, position protection execution, venue margin, order
atomicity, a $50 maximum realized loss, or permission to place an order.

No MEXC calls, database writes, credentials or submission transport.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from decimal import Decimal as D, InvalidOperation
from typing import Any

from app.t100_live_active_tpsl import effective_protective_quantity
from app.t100_live_execution import (
    EntryIntent, ExecutionHalted, modeled_short_loss_at_native_stop,
)


@dataclass(frozen=True)
class SharedCrossPilotObservation:
    status: str
    existing_account_positions: int | None = None
    existing_current_tpsl_rows: int | None = None
    existing_positions_without_observed_full_volume_stop: int | None = None
    existing_positions_with_unverified_cross_mode: int | None = None
    # This count is telemetry, NOT a requirement to add manual SL orders.
    modeled_new_trade_stop_loss_usdt: str | None = None
    risk_budget_usdt: str = "50"
    includes_manual_positions_in_collateral_risk: bool = True
    manual_positions_adopted_by_t100: bool = False
    cross_margin_liquidation_certified: bool = False
    native_stop_execution_certified: bool = False
    realized_loss_capped_at_budget: bool = False
    entry_allowed: bool = False
    live_authorization: bool = False
    mexc_orders_sent: bool = False
    read_only: bool = True


def _number(value: Any) -> D | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        d = D(str(value))
    except (InvalidOperation, TypeError, ValueError):
        return None
    return d if d.is_finite() and d > 0 else None


def _identity(value: Any) -> str | None:
    if value is None or isinstance(value, bool):
        return None
    s = str(value)
    return s if s and s not in {"0", "None"} else None


def _mode(value: Any) -> int | None:
    if type(value) is int and value in {1, 2}:
        return value
    if type(value) is str and value in {"1", "2"}:
        return int(value)
    return None


def assess_shared_cross_pilot_precheck(
    *, positions: Any, stops: Any, inventory: Any,
    proposed_intent: EntryIntent,
    loss_budget_usdt: D = D("50"),
) -> SharedCrossPilotObservation:
    """All-current-account observer; never permits bypassing real venue gates."""
    def blocked(code: str, *, missing: int | None = None,
                unknown_cross: int | None = None,
                modeled: D | None = None) -> SharedCrossPilotObservation:
        return SharedCrossPilotObservation(
            status=code,
            existing_account_positions=(
                len(positions) if type(positions) is list else None
            ),
            existing_current_tpsl_rows=(
                len(stops) if type(stops) is list else None
            ),
            existing_positions_without_observed_full_volume_stop=missing,
            existing_positions_with_unverified_cross_mode=unknown_cross,
            modeled_new_trade_stop_loss_usdt=(
                str(modeled) if modeled is not None else None
            ),
        )

    if (
        type(positions) is not list or type(stops) is not list
        or any(type(p) is not dict for p in positions)
        or any(type(s) is not dict for s in stops)
        or type(inventory) is not dict
        or type(proposed_intent) is not EntryIntent
        or type(loss_budget_usdt) is not D
        or not loss_budget_usdt.is_finite()
        or not D(0) < loss_budget_usdt <= D("50")
    ):
        return blocked("SHARED_CROSS_PRECHECK_INPUT_INVALID")
    if (
        inventory.get("account_wide_completeness_verified") is not True
        or inventory.get("critical_inventory_equal_across_witness_window")
        is not True
        or inventory.get("after_positions") != len(positions)
        or inventory.get("after_current_tpsl") != len(stops)
    ):
        return blocked("SHARED_CROSS_ACCOUNT_INVENTORY_UNVERIFIED")
    order_counts = inventory.get("open_order_counts")
    if (
        type(order_counts) is not dict
        or any(type(order_counts.get(k)) is not int
               for k in ("sumCount", "stopOrderCount", "limitOrderCount",
                         "planOrderCount", "trackOrderCount"))
        or order_counts["stopOrderCount"] != len(stops)
        or order_counts["sumCount"] != sum(
            order_counts[k] for k in (
                "stopOrderCount", "limitOrderCount",
                "planOrderCount", "trackOrderCount"
            )
        )
        or any(order_counts[k] for k in (
            "limitOrderCount", "planOrderCount", "trackOrderCount"
        ))
    ):
        return blocked("SHARED_CROSS_UNRELATED_OR_UNVERIFIED_PENDING_ORDERS")

    # Cap applies to all exchange positions, not just T100-owned positions.
    # Three manual positions leave at most one slot for the pilot.
    if len(positions) > 3:
        return blocked("SHARED_CROSS_ACCOUNT_SLOT_LIMIT")

    expected: dict[str, dict[str, Any]] = {}
    unknown_cross = 0
    for p in positions:
        pid = _identity(p.get("positionId") or p.get("id"))
        symbol = p.get("symbol")
        qty = _number(p.get("holdVol"))
        mode = _mode(p.get("openType"))
        side = _mode(p.get("positionType"))
        if (
            pid is None or pid in expected
            or type(symbol) is not str
            or re.fullmatch(r"[A-Z0-9]+_USDT", symbol) is None
            or qty is None or side is None
        ):
            return blocked("SHARED_CROSS_POSITION_IDENTITY_UNVERIFIED")
        if symbol == proposed_intent.symbol:
            return blocked("SHARED_CROSS_CANDIDATE_SYMBOL_ALREADY_OPEN")
        expected[pid] = {"symbol": symbol, "side": side, "qty": qty}
        if mode != 2:
            unknown_cross += 1

    # Never infer whole-position native protection from a TP-only order,
    # a record with a different side/symbol, inactive order, or a smaller
    # stop-loss volume. The conservative check needs ONE full-volume SL.
    observed_protection: set[str] = set()
    stop_ids: set[str] = set()
    for stop in stops:
        sid = _identity(stop.get("id") or stop.get("stopPlanOrderId"))
        pid = _identity(stop.get("positionId"))
        if (
            sid is None or sid in stop_ids or pid not in expected
            or stop.get("symbol") != expected[pid]["symbol"]
            or _mode(stop.get("positionType")) != expected[pid]["side"]
            or str(stop.get("state")) != "1"
            or str(stop.get("isFinished")) != "0"
        ):
            return blocked("SHARED_CROSS_TPSL_IDENTITY_OR_STATE_UNVERIFIED")
        stop_ids.add(sid)
        if _number(stop.get("stopLossPrice")) is None:
            continue  # TP-only is not an SL, even if it has full volume.
        volume = effective_protective_quantity(
            stop, position_quantity=expected[pid]["qty"],
        )
        if volume is not None and volume >= expected[pid]["qty"]:
            observed_protection.add(pid)

    missing = len(expected) - len(observed_protection)
    # Manual positions are operator-owned and can intentionally have no SL.
    # Their absence is an ACCOUNT RISK input, NOT a T100 stop violation.
    # No finite maximal loss can be inferred for unprotected cross shorts.
    # The T100-owned position stop requirement remains unchanged elsewhere.
    if unknown_cross:
        return blocked(
            "SHARED_CROSS_EXISTING_MARGIN_MODES_NOT_ALL_CROSS",
            missing=missing, unknown_cross=unknown_cross,
        )
    try:
        modeled = modeled_short_loss_at_native_stop(
            proposed_intent,
            max_adverse_slippage_bps=D("100"),
        )
    except (ExecutionHalted, TypeError, ValueError):
        return blocked(
            "SHARED_CROSS_PILOT_STOP_BUDGET_UNVERIFIED",
            missing=missing, unknown_cross=0,
        )
    if modeled >= loss_budget_usdt:
        return blocked(
            "SHARED_CROSS_PILOT_STOP_EXCEEDS_BUDGET",
            missing=missing, unknown_cross=0, modeled=modeled,
        )
    return blocked(
        (
            "SHARED_CROSS_MANUAL_UNBOUNDED_RISK_REQUIRES_ACCOUNT_STRESS"
            if missing
            else "SHARED_CROSS_MANUAL_POSITIONS_ACCOUNTED_NONAUTHORIZING"
        ),
        missing=missing, unknown_cross=0, modeled=modeled,
    )
