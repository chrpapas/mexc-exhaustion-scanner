"""Deterministic non-flat restart reconstruction for T100.

Pure/offline policy. No database writes, no exchange mutations, no order
submission, and no stop modification.

A non-flat position is recoverable only when:
- every managed position has a durable checkpoint,
- the originating intent is resolved and ACKed,
- no stop amendment is unresolved,
- exchange inventory is independently certified complete,
- the exchange position exactly matches identity/side/state/quantity,
- the expected native stop exists exactly once and matches identity/state,
  stop price and protective volume,
- checkpoint trailing state is internally coherent.

Anything else fails closed.
"""
from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from typing import Any, Protocol

from app.t100_live_supervisor import ManagedShort


class DurableRecoveryHalted(RuntimeError):
    pass


@dataclass(frozen=True)
class PositionCheckpoint:
    symbol: str
    external_oid: str
    exchange_order_id: str
    position_id: str
    contracts: Decimal
    entry_price: Decimal
    stop_order_id: str
    stop_price: Decimal
    best_profit_pct: Decimal
    armed: bool
    lifecycle_state: str
    checkpoint_version: int


@dataclass(frozen=True)
class DurableRecoveryReport:
    recovered: tuple[ManagedShort, ...]
    checkpoint_count: int
    exchange_position_count: int
    exchange_stop_count: int
    mode: str = "SHADOW_ONLY_RECOVERED_NONFLAT"
    live_authorization: bool = False


def _positive(raw: Any, label: str) -> Decimal:
    try:
        value = Decimal(str(raw))
    except (InvalidOperation, TypeError, ValueError) as exc:
        raise DurableRecoveryHalted(f"INVALID_{label}") from exc
    if not value.is_finite() or value <= 0:
        raise DurableRecoveryHalted(f"INVALID_{label}")
    return value


def _nonnegative(raw: Any, label: str) -> Decimal:
    try:
        value = Decimal(str(raw))
    except (InvalidOperation, TypeError, ValueError) as exc:
        raise DurableRecoveryHalted(f"INVALID_{label}") from exc
    if not value.is_finite() or value < 0:
        raise DurableRecoveryHalted(f"INVALID_{label}")
    return value


def _integer(raw: Any, label: str) -> int:
    if isinstance(raw, bool):
        raise DurableRecoveryHalted(f"INVALID_{label}")
    try:
        value = int(str(raw))
    except (TypeError, ValueError) as exc:
        raise DurableRecoveryHalted(f"INVALID_{label}") from exc
    if str(value) != str(raw):
        raise DurableRecoveryHalted(f"INVALID_{label}")
    return value


def checkpoint_from_row(row: Any) -> PositionCheckpoint:
    if not isinstance(row, dict):
        raise DurableRecoveryHalted("MALFORMED_CHECKPOINT")
    required_text = (
        "symbol", "external_oid", "exchange_order_id",
        "position_id", "stop_order_id",
    )
    values: dict[str, str] = {}
    for field in required_text:
        raw = row.get(field)
        if not isinstance(raw, str) or not raw:
            raise DurableRecoveryHalted("CHECKPOINT_IDENTITY_MISSING")
        values[field] = raw

    armed = row.get("armed")
    if type(armed) is not bool:
        raise DurableRecoveryHalted("INVALID_CHECKPOINT_ARMED")

    lifecycle = row.get("lifecycle_state")
    if lifecycle != "OPEN":
        raise DurableRecoveryHalted("CHECKPOINT_NOT_OPEN")

    version = _integer(row.get("checkpoint_version"), "CHECKPOINT_VERSION")
    if version <= 0:
        raise DurableRecoveryHalted("INVALID_CHECKPOINT_VERSION")

    best = _nonnegative(row.get("best_profit_pct"), "BEST_PROFIT_PCT")
    if armed != (best >= Decimal("14")):
        raise DurableRecoveryHalted("CHECKPOINT_TRAILING_STATE_INCOHERENT")

    return PositionCheckpoint(
        symbol=values["symbol"],
        external_oid=values["external_oid"],
        exchange_order_id=values["exchange_order_id"],
        position_id=values["position_id"],
        contracts=_positive(row.get("contracts"), "CHECKPOINT_CONTRACTS"),
        entry_price=_positive(row.get("entry_price"), "CHECKPOINT_ENTRY_PRICE"),
        stop_order_id=values["stop_order_id"],
        stop_price=_positive(row.get("stop_price"), "CHECKPOINT_STOP_PRICE"),
        best_profit_pct=best,
        armed=armed,
        lifecycle_state=lifecycle,
        checkpoint_version=version,
    )


def _validate_intents(
    checkpoints: tuple[PositionCheckpoint, ...],
    intents: Any,
) -> None:
    if not isinstance(intents, list) or any(not isinstance(x, dict) for x in intents):
        raise DurableRecoveryHalted("INTENT_LEDGER_RESPONSE_AMBIGUOUS")

    by_oid: dict[str, dict[str, Any]] = {}
    for row in intents:
        oid = row.get("external_oid")
        if not isinstance(oid, str) or not oid or oid in by_oid:
            raise DurableRecoveryHalted("INTENT_LEDGER_IDENTITY_INVALID")
        by_oid[oid] = row

    checkpoint_oids = {c.external_oid for c in checkpoints}
    unresolved = [
        row for row in intents
        if row.get("state") in {"PREPARED", "SUBMITTING", "UNKNOWN", "REVIEW"}
    ]
    if unresolved:
        raise DurableRecoveryHalted("UNRESOLVED_ORDER_INTENT")

    for cp in checkpoints:
        row = by_oid.get(cp.external_oid)
        if row is None:
            raise DurableRecoveryHalted("CHECKPOINT_INTENT_MISSING")
        if row.get("state") != "ACK":
            raise DurableRecoveryHalted("CHECKPOINT_INTENT_NOT_ACK")
        if str(row.get("exchange_order_id") or "") != cp.exchange_order_id:
            raise DurableRecoveryHalted("CHECKPOINT_ORDER_ID_DRIFT")
        if str(row.get("symbol") or "") != cp.symbol:
            raise DurableRecoveryHalted("CHECKPOINT_SYMBOL_DRIFT")
        try:
            volume = _positive(row.get("volume_contracts"), "INTENT_VOLUME")
            stop_price = _positive(row.get("stop_price"), "INTENT_STOP_PRICE")
        except DurableRecoveryHalted:
            raise
        if volume != cp.contracts:
            raise DurableRecoveryHalted("CHECKPOINT_INTENT_VOLUME_DRIFT")
        # The live native stop may only tighten from the original intent.
        if cp.stop_price > stop_price:
            raise DurableRecoveryHalted("CHECKPOINT_STOP_LOOSER_THAN_ENTRY_INTENT")

    # Any ACK intent not represented by an OPEN checkpoint is ambiguous.
    extra_ack = [
        row for oid, row in by_oid.items()
        if row.get("state") == "ACK" and oid not in checkpoint_oids
    ]
    if extra_ack:
        raise DurableRecoveryHalted("ACK_INTENT_WITHOUT_OPEN_CHECKPOINT")


def _validate_stop_amendments(stop_amendments: Any) -> None:
    if (
        not isinstance(stop_amendments, list)
        or any(not isinstance(x, dict) for x in stop_amendments)
    ):
        raise DurableRecoveryHalted("STOP_AMENDMENT_LEDGER_RESPONSE_AMBIGUOUS")
    if any(row.get("state") != "VERIFIED" for row in stop_amendments):
        raise DurableRecoveryHalted("UNRESOLVED_STOP_AMENDMENT")


def recover_nonflat(
    *,
    checkpoint_rows: Any,
    intent_rows: Any,
    stop_amendment_rows: Any,
    orders: Any,
    positions: Any,
    stops: Any,
    positions_complete: bool,
    stops_complete: bool,
) -> DurableRecoveryReport:
    """Rebuild ManagedShort state only from durable + exact exchange evidence."""
    if positions_complete is not True or stops_complete is not True:
        raise DurableRecoveryHalted("EXCHANGE_INVENTORY_COMPLETENESS_UNVERIFIED")
    if (
        not isinstance(checkpoint_rows, list)
        or not isinstance(orders, list)
        or not isinstance(positions, list)
        or not isinstance(stops, list)
        or any(not isinstance(x, dict) for x in orders)
        or any(not isinstance(x, dict) for x in positions)
        or any(not isinstance(x, dict) for x in stops)
    ):
        raise DurableRecoveryHalted("RECOVERY_INPUT_RESPONSE_AMBIGUOUS")

    checkpoints = tuple(checkpoint_from_row(row) for row in checkpoint_rows)
    if not 1 <= len(checkpoints) <= 4:
        raise DurableRecoveryHalted("NONFLAT_CHECKPOINT_COUNT_INVALID")
    if len({c.position_id for c in checkpoints}) != len(checkpoints):
        raise DurableRecoveryHalted("DUPLICATE_CHECKPOINT_POSITION")
    if len({c.symbol for c in checkpoints}) != len(checkpoints):
        raise DurableRecoveryHalted("DUPLICATE_CHECKPOINT_SYMBOL")
    if len({c.stop_order_id for c in checkpoints}) != len(checkpoints):
        raise DurableRecoveryHalted("DUPLICATE_CHECKPOINT_STOP")

    _validate_intents(checkpoints, intent_rows)
    _validate_stop_amendments(stop_amendment_rows)

    if len(orders) != len(checkpoints):
        raise DurableRecoveryHalted("EXCHANGE_ORDER_COUNT_MISMATCH")
    orders_by_external: dict[str, dict[str, Any]] = {}
    for row in orders:
        external = row.get("externalOid")
        if not isinstance(external, str) or not external or external in orders_by_external:
            raise DurableRecoveryHalted("EXCHANGE_ORDER_IDENTITY_INVALID")
        orders_by_external[external] = row

    for cp in checkpoints:
        order = orders_by_external.get(cp.external_oid)
        if order is None:
            raise DurableRecoveryHalted("CHECKPOINT_ORDER_NOT_FOUND")
        if str(order.get("orderId") or "") != cp.exchange_order_id:
            raise DurableRecoveryHalted("RECOVERED_ORDER_ID_DRIFT")
        if str(order.get("symbol") or "") != cp.symbol:
            raise DurableRecoveryHalted("RECOVERED_ORDER_SYMBOL_DRIFT")
        if _integer(order.get("side"), "ORDER_SIDE") != 3:
            raise DurableRecoveryHalted("RECOVERED_ORDER_SIDE_DRIFT")
        if _integer(order.get("state"), "ORDER_STATE") != 3:
            raise DurableRecoveryHalted("RECOVERED_ORDER_NOT_FINAL_FILLED")
        if str(order.get("positionId") or "") != cp.position_id:
            raise DurableRecoveryHalted("RECOVERED_ORDER_POSITION_ID_DRIFT")
        if _positive(order.get("dealVol"), "ORDER_DEAL_VOLUME") != cp.contracts:
            raise DurableRecoveryHalted("RECOVERED_ORDER_VOLUME_DRIFT")
        avg = _positive(order.get("dealAvgPrice"), "ORDER_AVERAGE_PRICE")
        tolerance = max(
            Decimal("0.00000001"),
            cp.entry_price * Decimal("0.00000001"),
        )
        if abs(avg - cp.entry_price) > tolerance:
            raise DurableRecoveryHalted("RECOVERED_ORDER_AVERAGE_PRICE_DRIFT")

    if set(orders_by_external) != {c.external_oid for c in checkpoints}:
        raise DurableRecoveryHalted("UNRECOGNIZED_EXCHANGE_ORDER")

    if len(positions) != len(checkpoints):
        raise DurableRecoveryHalted("EXCHANGE_POSITION_COUNT_MISMATCH")
    if len(stops) != len(checkpoints):
        raise DurableRecoveryHalted("EXCHANGE_STOP_COUNT_MISMATCH")

    live_positions: dict[tuple[str, str], dict[str, Any]] = {}
    for row in positions:
        symbol = row.get("symbol")
        pid = str(row.get("positionId") or row.get("id") or "")
        if not isinstance(symbol, str) or not symbol or not pid:
            raise DurableRecoveryHalted("EXCHANGE_POSITION_IDENTITY_INVALID")
        key = (symbol, pid)
        if key in live_positions:
            raise DurableRecoveryHalted("DUPLICATE_EXCHANGE_POSITION")
        live_positions[key] = row

    live_stops: dict[tuple[str, str], dict[str, Any]] = {}
    for row in stops:
        pid = str(row.get("positionId") or "")
        sid = str(row.get("id") or row.get("stopPlanOrderId") or "")
        if not pid or not sid:
            raise DurableRecoveryHalted("EXCHANGE_STOP_IDENTITY_INVALID")
        key = (pid, sid)
        if key in live_stops:
            raise DurableRecoveryHalted("DUPLICATE_EXCHANGE_STOP")
        live_stops[key] = row

    recovered: list[ManagedShort] = []
    for cp in checkpoints:
        pos = live_positions.get((cp.symbol, cp.position_id))
        if pos is None:
            raise DurableRecoveryHalted("CHECKPOINT_POSITION_NOT_FOUND")
        if _integer(pos.get("positionType"), "POSITION_SIDE") != 2:
            raise DurableRecoveryHalted("RECOVERED_POSITION_SIDE_DRIFT")
        if _integer(pos.get("state"), "POSITION_STATE") != 1:
            raise DurableRecoveryHalted("RECOVERED_POSITION_STATE_DRIFT")
        if _positive(pos.get("holdVol"), "POSITION_VOLUME") != cp.contracts:
            raise DurableRecoveryHalted("RECOVERED_POSITION_VOLUME_DRIFT")

        hold_avg = _positive(pos.get("holdAvgPrice"), "POSITION_ENTRY_PRICE")
        price_tolerance = max(
            Decimal("0.00000001"),
            cp.entry_price * Decimal("0.00000001"),
        )
        if abs(hold_avg - cp.entry_price) > price_tolerance:
            raise DurableRecoveryHalted("RECOVERED_POSITION_ENTRY_PRICE_DRIFT")

        stop = live_stops.get((cp.position_id, cp.stop_order_id))
        if stop is None:
            raise DurableRecoveryHalted("CHECKPOINT_STOP_NOT_FOUND")
        if _integer(stop.get("state"), "STOP_STATE") != 1:
            raise DurableRecoveryHalted("RECOVERED_STOP_NOT_ACTIVE")
        if stop.get("symbol") not in (None, cp.symbol):
            raise DurableRecoveryHalted("RECOVERED_STOP_SYMBOL_DRIFT")
        if _positive(stop.get("stopLossPrice"), "STOP_PRICE") != cp.stop_price:
            raise DurableRecoveryHalted("RECOVERED_STOP_PRICE_DRIFT")
        if _positive(stop.get("vol"), "STOP_VOLUME") != cp.contracts:
            raise DurableRecoveryHalted("RECOVERED_STOP_VOLUME_DRIFT")

        recovered.append(
            ManagedShort(
                cp.symbol,
                cp.position_id,
                cp.contracts,
                cp.entry_price,
                cp.stop_order_id,
                cp.stop_price,
                cp.best_profit_pct,
                cp.armed,
            )
        )

    expected_position_keys = {(c.symbol, c.position_id) for c in checkpoints}
    if set(live_positions) != expected_position_keys:
        raise DurableRecoveryHalted("UNRECOGNIZED_EXCHANGE_POSITION")

    expected_stop_keys = {(c.position_id, c.stop_order_id) for c in checkpoints}
    if set(live_stops) != expected_stop_keys:
        raise DurableRecoveryHalted("UNRECOGNIZED_EXCHANGE_STOP")

    return DurableRecoveryReport(
        recovered=tuple(recovered),
        checkpoint_count=len(checkpoints),
        exchange_position_count=len(positions),
        exchange_stop_count=len(stops),
    )

class RecoveryLedger(Protocol):
    async def fetch(self, query: str) -> list[Any]: ...


class RecoveryExchange(Protocol):
    async def order_by_external_oid(self, symbol: str, external_oid: str) -> dict[str, Any] | None: ...
    async def open_positions(self, symbol: str | None = None) -> list[dict[str, Any]]: ...
    async def open_stop_orders(self, symbol: str | None = None) -> list[dict[str, Any]]: ...


class DurableRestartRecoveryGate:
    """GET/SELECT-only orchestration around the pure recovery policy.

    The caller must independently establish endpoint completeness and pass
    those booleans explicitly. This class never upgrades an ordinary MEXC
    list response into completeness evidence.
    """

    def __init__(self, ledger: RecoveryLedger, exchange: RecoveryExchange):
        self.ledger = ledger
        self.exchange = exchange

    async def inspect_nonflat(
        self,
        *,
        positions_complete: bool = False,
        stops_complete: bool = False,
    ) -> DurableRecoveryReport:
        try:
            checkpoints = await self.ledger.fetch(
                """SELECT symbol, external_oid, exchange_order_id, position_id,
                          contracts, entry_price, stop_order_id, stop_price,
                          best_profit_pct, armed, lifecycle_state,
                          checkpoint_version
                   FROM t100_live_position_checkpoints
                   WHERE lifecycle_state <> 'CLOSED'
                   ORDER BY symbol, position_id"""
            )
            intents = await self.ledger.fetch(
                """SELECT symbol, external_oid, exchange_order_id,
                          volume_contracts, stop_price, state
                   FROM t100_live_order_intents
                   ORDER BY id"""
            )
            amendments = await self.ledger.fetch(
                """SELECT external_id, position_id, stop_order_id, state
                   FROM t100_live_stop_amendments
                   ORDER BY id"""
            )
        except Exception as exc:
            raise DurableRecoveryHalted("RECOVERY_LEDGER_UNAVAILABLE") from exc

        try:
            parsed_checkpoints = tuple(
                checkpoint_from_row(row) for row in checkpoints
            )
            orders: list[dict[str, Any]] = []
            for cp in parsed_checkpoints:
                order = await self.exchange.order_by_external_oid(
                    cp.symbol, cp.external_oid
                )
                if not isinstance(order, dict):
                    raise DurableRecoveryHalted(
                        "RECOVERY_ORIGIN_ORDER_UNAVAILABLE"
                    )
                orders.append(order)
            positions = await self.exchange.open_positions()
            stops = await self.exchange.open_stop_orders()
        except DurableRecoveryHalted:
            raise
        except Exception as exc:
            raise DurableRecoveryHalted("RECOVERY_EXCHANGE_READ_UNAVAILABLE") from exc

        return recover_nonflat(
            checkpoint_rows=checkpoints,
            intent_rows=intents,
            stop_amendment_rows=amendments,
            orders=orders,
            positions=positions,
            stops=stops,
            positions_complete=positions_complete,
            stops_complete=stops_complete,
        )

