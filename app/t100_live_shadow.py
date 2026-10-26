"""Single-snapshot, read-only T100 shadow reconciliation.

Consumes ONE independently complete inventory observation; unlike the
asynchronous supervisor, it cannot mix two different account snapshots.
Never submits, modifies, cancels or authorizes a real order.
"""
from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import Any, Sequence

from app.t100_live_active_tpsl import effective_protective_quantity
from app.t100_live_snapshot import validate_inventory_snapshot
from app.t100_live_supervisor import ManagedShort
from app.t100_live_trailing import next_short_stop


class ShadowHalted(RuntimeError):
    pass


@dataclass(frozen=True)
class ShadowResult:
    mode: str
    observed_at_ms: int
    position_count: int
    protected_positions: int
    proposals: tuple[tuple[str, str, Decimal], ...]
    reason: str


def shadow_reconcile(
    *, positions: Any, stops: Any, expected: Sequence[ManagedShort],
    observed_at_ms: int, now_ms: int,
    positions_complete: bool, stops_complete: bool,
    observed_lows: dict[str, Decimal] | None = None,
    price_ticks: dict[str, Decimal] | None = None,
) -> ShadowResult:
    """Read-only reconciliation; complete flags must come from verified transport."""
    snapshot = validate_inventory_snapshot(
        positions=positions, stops=stops,
        observed_at_ms=observed_at_ms, now_ms=now_ms,
        positions_complete=positions_complete, stops_complete=stops_complete,
    )
    if len(expected) > 4:
        raise ShadowHalted("POSITION_SLOT_LIMIT")
    keys = [(p.symbol, p.position_id) for p in expected]
    if len(keys) != len(set(keys)) or len({p.symbol for p in expected}) != len(expected):
        raise ShadowHalted("DUPLICATE_EXPECTED_POSITION")
    if len(expected) != snapshot.position_count or len(expected) != snapshot.stop_count:
        raise ShadowHalted("EXPECTED_INVENTORY_COUNT_MISMATCH")
    actual_positions = {(p["symbol"], str(p.get("positionId") or p.get("id"))): p
                        for p in positions}
    actual_stops = {(str(s["positionId"]), str(s.get("id") or s.get("stopPlanOrderId"))): s
                    for s in stops}
    proposals = []
    lows = observed_lows or {}
    ticks = price_ticks or {}
    if set(lows) != set(ticks):
        raise ShadowHalted("INCOMPLETE_MARKET_DATA_PAIR")
    for wanted in expected:
        if not wanted.symbol or not wanted.position_id or not wanted.stop_order_id:
            raise ShadowHalted("INVALID_EXPECTED_IDENTITY")
        for value in (wanted.contracts, wanted.entry_price, wanted.stop_price):
            if not isinstance(value, Decimal) or not value.is_finite() or value <= 0:
                raise ShadowHalted("INVALID_EXPECTED_QUANTITY_OR_PRICE")
        if not isinstance(wanted.best_profit_pct, Decimal) or not wanted.best_profit_pct.is_finite():
            raise ShadowHalted("INVALID_TRAILING_STATE")
        p = actual_positions.get((wanted.symbol, wanted.position_id))
        s = actual_stops.get((wanted.position_id, wanted.stop_order_id))
        if p is None or s is None:
            raise ShadowHalted("UNKNOWN_POSITION_OR_STOP")
        if int(p["positionType"]) != 2 or Decimal(str(p["holdVol"])) != wanted.contracts:
            raise ShadowHalted("POSITION_SIDE_OR_VOLUME_DRIFT")
        effective_qty = effective_protective_quantity(
            s,
            position_quantity=wanted.contracts,
        )
        if effective_qty is None or effective_qty < wanted.contracts:
            raise ShadowHalted("INSUFFICIENT_NATIVE_STOP_COVERAGE")
        if Decimal(str(s["stopLossPrice"])) > wanted.stop_price:
            raise ShadowHalted("NATIVE_STOP_LOOSENED")
        if wanted.symbol in lows:
            step = next_short_stop(
                entry=wanted.entry_price, low=lows[wanted.symbol],
                current_stop=Decimal(str(s["stopLossPrice"])),
                best_profit_pct=wanted.best_profit_pct,
                price_tick=ticks[wanted.symbol], armed=wanted.armed,
            )
            if step.changed:
                proposals.append((wanted.symbol, wanted.stop_order_id, step.stop_price))
    if set(actual_positions) != set(keys):
        raise ShadowHalted("UNEXPECTED_ACCOUNT_POSITION")
    expected_stops = {(p.position_id, p.stop_order_id) for p in expected}
    if set(actual_stops) != expected_stops:
        raise ShadowHalted("UNEXPECTED_ACCOUNT_STOP")
    if set(lows) - {p.symbol for p in expected}:
        raise ShadowHalted("UNRECOGNIZED_MARKET_DATA_SYMBOL")
    return ShadowResult(
        "SHADOW_ONLY", observed_at_ms, snapshot.position_count,
        len(expected), tuple(proposals), "READ_ONLY_RECONCILED_NOT_TRADE_AUTHORIZATION",
    )
