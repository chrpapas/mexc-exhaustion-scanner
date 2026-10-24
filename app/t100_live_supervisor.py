"""Read-only restart reconciliation and trailing decision supervisor.

No order placement, stop modification or cancellation is performed here.
Unknown exchange inventory and incomplete fills stop the supervisor.
"""
from __future__ import annotations
from dataclasses import dataclass
from decimal import Decimal
from typing import Protocol

from app.t100_live_trailing import next_short_stop


class SupervisionHalted(RuntimeError):
    pass


@dataclass(frozen=True)
class ManagedShort:
    symbol: str
    position_id: str
    contracts: Decimal
    entry_price: Decimal
    stop_order_id: str
    stop_price: Decimal
    best_profit_pct: Decimal
    armed: bool


@dataclass(frozen=True)
class StopProposal:
    symbol: str
    position_id: str
    stop_order_id: str
    old_stop: Decimal
    proposed_stop: Decimal
    best_profit_pct: Decimal
    armed: bool


class InventoryReader(Protocol):
    async def open_positions(self, symbol: str | None = None) -> list[dict]: ...
    async def open_stop_orders(self, symbol: str | None = None) -> list[dict]: ...


def _decimal(value: object) -> Decimal:
    try:
        x = Decimal(str(value))
    except Exception as exc:
        raise SupervisionHalted("INVALID_EXCHANGE_NUMERIC_FIELD") from exc
    if not x.is_finite() or x <= 0:
        raise SupervisionHalted("INVALID_EXCHANGE_NUMERIC_FIELD")
    return x


class LivePositionSupervisor:
    def __init__(self, exchange: InventoryReader):
        self.exchange = exchange

    async def verify(self, expected: list[ManagedShort]) -> None:
        """Verify all exchange shorts have corresponding locally known protected inventory.

        Does not assume that an empty response is a valid account state.
        """
        try:
            positions = await self.exchange.open_positions()
            stops = await self.exchange.open_stop_orders()
        except Exception as exc:
            raise SupervisionHalted("EXCHANGE_READ_UNAVAILABLE") from exc
        if not isinstance(positions, list) or not isinstance(stops, list):
            raise SupervisionHalted("AMBIGUOUS_EXCHANGE_RESPONSE")
        if len(expected) > 4:
            raise SupervisionHalted("POSITION_SLOT_LIMIT")
        keys = {(p.symbol, p.position_id) for p in expected}
        if len(keys) != len(expected):
            raise SupervisionHalted("DUPLICATE_EXPECTED_INVENTORY")
        if len({p.symbol for p in expected}) != len(expected):
            raise SupervisionHalted("ONE_POSITION_PER_SYMBOL")
        for wanted in expected:
            if (not wanted.symbol or not wanted.position_id or not wanted.stop_order_id
                    or not isinstance(wanted.contracts, Decimal)
                    or not isinstance(wanted.entry_price, Decimal)
                    or not isinstance(wanted.stop_price, Decimal)):
                raise SupervisionHalted("INVALID_EXPECTED_POSITION")
            _decimal(wanted.contracts)
            _decimal(wanted.entry_price)
            _decimal(wanted.stop_price)
        live_keys = set()
        for p in positions:
            if not isinstance(p, dict):
                raise SupervisionHalted("MALFORMED_POSITION")
            key = (str(p.get("symbol")), str(p.get("positionId") or p.get("id") or ""))
            if key in live_keys:
                raise SupervisionHalted("DUPLICATE_EXCHANGE_POSITION")
            live_keys.add(key)
            if key not in keys:
                raise SupervisionHalted("UNRECOGNIZED_EXCHANGE_POSITION")
            wanted = next(x for x in expected if (x.symbol, x.position_id) == key)
            # A venue may report a closed position in a response; only active
            # inventory is expected from open_positions.
            actual = _decimal(p.get("holdVol"))
            if actual != wanted.contracts:
                raise SupervisionHalted("POSITION_VOLUME_DRIFT")
            if int(p.get("positionType", -1)) != 2:
                raise SupervisionHalted("POSITION_SIDE_DRIFT")
        if live_keys != keys:
            raise SupervisionHalted("MISSING_EXCHANGE_POSITION")
        expected_stop_ids = {(p.position_id, p.stop_order_id) for p in expected}
        if len(expected_stop_ids) != len(expected):
            raise SupervisionHalted("DUPLICATE_EXPECTED_STOP")
        actual_stop_ids = set()
        for s in stops:
            if not isinstance(s, dict):
                raise SupervisionHalted("MALFORMED_STOP")
            sid = (str(s.get("positionId") or ""),
                   str(s.get("id") or s.get("stopPlanOrderId") or ""))
            if sid in actual_stop_ids:
                raise SupervisionHalted("DUPLICATE_EXCHANGE_STOP")
            actual_stop_ids.add(sid)
            if sid not in expected_stop_ids:
                raise SupervisionHalted("UNRECOGNIZED_EXCHANGE_STOP")
        if actual_stop_ids != expected_stop_ids:
            raise SupervisionHalted("NATIVE_STOP_MISSING_OR_DUPLICATE")
        for wanted in expected:
            matched = [
                s for s in stops
                if isinstance(s, dict)
                and str(s.get("positionId")) == wanted.position_id
                and str(s.get("id") or s.get("stopPlanOrderId")) == wanted.stop_order_id
                and int(s.get("state", 0)) == 1
            ]
            if len(matched) != 1:
                raise SupervisionHalted("NATIVE_STOP_MISSING_OR_DUPLICATE")
            stop = matched[0]
            if (_decimal(stop.get("stopLossPrice")) > wanted.stop_price
                    or _decimal(stop.get("vol")) < wanted.contracts):
                raise SupervisionHalted("STOP_PRICE_OR_COVERAGE_DRIFT")

    def propose(self, position: ManagedShort, *, observed_low: Decimal,
                price_tick: Decimal) -> StopProposal | None:
        step = next_short_stop(
            entry=position.entry_price,
            low=observed_low,
            current_stop=position.stop_price,
            best_profit_pct=position.best_profit_pct,
            price_tick=price_tick,
            armed=position.armed,
        )
        if not step.changed:
            return None
        return StopProposal(
            position.symbol, position.position_id, position.stop_order_id,
            position.stop_price, step.stop_price, step.best_profit_pct,
            step.active,
        )
