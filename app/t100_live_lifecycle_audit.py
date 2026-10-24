"""Read-only MEXC lifecycle inspection for draft T100 live readiness.

No order, stop, or position mutations. Unknown/partial responses halt instead
of being interpreted as a flat, protected account. This is a prerequisite
for, not an implementation of, an autonomous execution worker.
"""
from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from typing import Any, Protocol


class LifecycleHalted(RuntimeError):
    pass


@dataclass(frozen=True)
class TrackedOrder:
    symbol: str
    external_oid: str
    exchange_order_id: str
    expected_contracts: Decimal
    position_id: str | None = None


@dataclass(frozen=True)
class FillSnapshot:
    symbol: str
    external_oid: str
    exchange_order_id: str
    ordered_contracts: Decimal
    filled_contracts: Decimal
    unfilled_contracts: Decimal
    average_fill_price: Decimal | None
    exchange_state: int
    position_id: str | None
    stop_covered: bool
    status: str


class ReadOnlyExchange(Protocol):
    async def order_by_external_oid(self, symbol: str, external_oid: str) -> dict[str, Any] | None: ...
    async def open_positions(self, symbol: str | None = None) -> list[dict[str, Any]]: ...
    async def open_stop_orders(self, symbol: str | None = None) -> list[dict[str, Any]]: ...


def dec(value: object, *, allow_zero: bool = False) -> Decimal:
    try:
        n = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError) as exc:
        raise LifecycleHalted("MISSING_OR_INVALID_NUMERIC_FIELD") from exc
    if not n.is_finite() or n < 0 or (n == 0 and not allow_zero):
        raise LifecycleHalted("MISSING_OR_INVALID_NUMERIC_FIELD")
    return n


def integer(value: object) -> int:
    if isinstance(value, bool):
        raise LifecycleHalted("INVALID_STATE")
    try:
        n = int(str(value))
    except (ValueError, TypeError) as exc:
        raise LifecycleHalted("INVALID_STATE") from exc
    if str(n) != str(value):
        raise LifecycleHalted("INVALID_STATE")
    return n


class LifecycleAuditor:
    def __init__(self, exchange: ReadOnlyExchange):
        self.exchange = exchange

    async def inspect(self, tracked: TrackedOrder) -> FillSnapshot:
        if not tracked.symbol or not tracked.external_oid or not tracked.exchange_order_id:
            raise LifecycleHalted("INCOMPLETE_TRACKING_IDENTITY")
        expected = dec(tracked.expected_contracts)
        try:
            order = await self.exchange.order_by_external_oid(tracked.symbol, tracked.external_oid)
            positions = await self.exchange.open_positions()
            stops = await self.exchange.open_stop_orders()
        except Exception as exc:
            raise LifecycleHalted("EXCHANGE_READ_FAILED") from exc
        if not isinstance(order, dict) or not isinstance(positions, list) or not isinstance(stops, list):
            raise LifecycleHalted("UNVERIFIABLE_EXCHANGE_RESPONSE")
        if (str(order.get("symbol")) != tracked.symbol
                or str(order.get("externalOid")) != tracked.external_oid
                or str(order.get("orderId")) != tracked.exchange_order_id
                or integer(order.get("side")) != 3):
            raise LifecycleHalted("ORDER_IDENTITY_OR_SIDE_MISMATCH")
        actual_ordered = dec(order.get("vol"))
        if actual_ordered != expected:
            raise LifecycleHalted("ORDER_CONTRACT_COUNT_DRIFT")
        filled = dec(order.get("dealVol"), allow_zero=True)
        if filled > expected:
            raise LifecycleHalted("OVERFILL_OR_INVALID_FILL")
        state = integer(order.get("state"))
        if state not in {1, 2, 3, 4, 5, 6}:
            raise LifecycleHalted("UNKNOWN_EXCHANGE_ORDER_STATE")
        position_id = str(order.get("positionId") or tracked.position_id or "") or None
        if tracked.position_id and position_id != tracked.position_id:
            raise LifecycleHalted("POSITION_ID_DRIFT")
        avg_raw = order.get("dealAvgPrice")
        avg = None if filled == 0 else dec(avg_raw)
        if filled == 0 and avg_raw not in (None, "", 0, "0"):
            raise LifecycleHalted("PRICE_WITHOUT_FILL")
        # A single-order audit must never overlook unrelated account inventory.
        # Any additional position is an unresolved account-wide exposure.
        for p in positions:
            if not isinstance(p, dict):
                raise LifecycleHalted("MALFORMED_ACCOUNT_POSITION")
            if filled == 0 or str(p.get("positionId") or p.get("id")) != position_id or str(p.get("symbol")) != tracked.symbol:
                raise LifecycleHalted("UNRECOGNIZED_ACCOUNT_POSITION")
        for s in stops:
            if not isinstance(s, dict):
                raise LifecycleHalted("MALFORMED_ACCOUNT_STOP")
            if filled == 0 or str(s.get("positionId")) != position_id:
                raise LifecycleHalted("UNRECOGNIZED_ACCOUNT_STOP")
        covered = False
        if filled:
            if not position_id:
                raise LifecycleHalted("FILL_WITHOUT_POSITION_ID")
            matching_positions = [
                p for p in positions if isinstance(p, dict)
                and str(p.get("positionId") or p.get("id")) == position_id
                and str(p.get("symbol")) == tracked.symbol
            ]
            if len(matching_positions) != 1:
                raise LifecycleHalted("FILLED_POSITION_NOT_UNIQUELY_FOUND")
            pos = matching_positions[0]
            if integer(pos.get("positionType")) != 2:
                raise LifecycleHalted("FILLED_POSITION_SIDE_DRIFT")
            holding = dec(pos.get("holdVol"))
            if holding < filled:
                raise LifecycleHalted("FILL_EXCEEDS_POSITION_HOLDING")
            matching_stops = [
                s for s in stops if isinstance(s, dict)
                and str(s.get("positionId")) == position_id
                and integer(s.get("state")) == 1
                and dec(s.get("stopLossPrice")) > 0
                and dec(s.get("vol")) >= holding
            ]
            if len(matching_stops) != 1:
                raise LifecycleHalted("FILLED_POSITION_NOT_UNIQUELY_PROTECTED")
            covered = True
        else:
            if position_id:
                raise LifecycleHalted("POSITION_ID_WITHOUT_FILL")
        # Unfilled or partial orders are never cleared for another entry.
        if filled == 0:
            status = "ACK_UNFILLED_BLOCK_NEW_ENTRY"
        elif filled < expected:
            status = "PARTIAL_PROTECTED_BLOCK_NEW_ENTRY"
        elif state != 3:
            status = "FILLED_BUT_NONFINAL_BLOCK_NEW_ENTRY"
        else:
            status = "FILLED_PROTECTED_NEEDS_FEE_FUNDING_RECONCILIATION"
        return FillSnapshot(
            tracked.symbol, tracked.external_oid, tracked.exchange_order_id,
            expected, filled, expected - filled, avg, state, position_id,
            covered, status,
        )
