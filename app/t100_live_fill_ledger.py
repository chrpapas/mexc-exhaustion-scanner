"""Durable verified order-fill sets for T100.

A fill set may be persisted only after the order-scoped MEXC deal-details
endpoint reconciles exactly to the immutable parent order. No exchange
mutations occur here.

Rows are append-only. Re-observing the same order is idempotent only when the
canonical digest is identical; a different fill set for an already verified
order fails closed.
"""
from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
import hashlib
import json
from typing import Any, Protocol

from app.t100_live_fill_probe import assess_order_deal_reconciliation


class VerifiedFillLedgerHalted(RuntimeError):
    pass


SIDE_LABELS = {2: "CLOSE_SHORT", 3: "OPEN_SHORT"}


def _decimal(raw: Any, *, positive: bool = False) -> Decimal:
    try:
        value = Decimal(str(raw))
    except (InvalidOperation, TypeError, ValueError) as exc:
        raise VerifiedFillLedgerHalted("INVALID_FILL_NUMERIC") from exc
    if not value.is_finite() or (positive and value <= 0):
        raise VerifiedFillLedgerHalted("INVALID_FILL_NUMERIC")
    return value


def _integer(raw: Any) -> int:
    if isinstance(raw, bool):
        raise VerifiedFillLedgerHalted("INVALID_FILL_INTEGER")
    try:
        value = int(str(raw))
    except (TypeError, ValueError) as exc:
        raise VerifiedFillLedgerHalted("INVALID_FILL_INTEGER") from exc
    if str(value) != str(raw):
        raise VerifiedFillLedgerHalted("INVALID_FILL_INTEGER")
    return value


@dataclass(frozen=True)
class VerifiedFill:
    trade_id: str
    order_id: str
    position_id: str
    side: str
    contracts: Decimal
    price: Decimal
    fee_usdt: Decimal
    timestamp_ms: int
    category: int


@dataclass(frozen=True)
class VerifiedOrderFillSet:
    order_id: str
    position_id: str
    symbol: str
    side: str
    order_state: int
    deal_volume: Decimal
    average_price: Decimal
    fee_currency: str
    fills: tuple[VerifiedFill, ...]
    fill_digest_sha256: str
    final_order_fill_history_complete: bool
    strategy_fully_filled_order_complete: bool


class VerifiedFillStore(Protocol):
    async def persist(self, fill_set: VerifiedOrderFillSet) -> str: ...


def build_verified_order_fill_set(
    *,
    order_id: str,
    order: Any,
    fills: Any,
    require_strategy_fully_filled: bool = True,
) -> VerifiedOrderFillSet:
    report = assess_order_deal_reconciliation(order_id, order, fills)
    if report.get("final_order_fill_history_complete") is not True:
        raise VerifiedFillLedgerHalted(
            "ORDER_FILL_HISTORY_NOT_FINAL_COMPLETE"
        )
    if (
        require_strategy_fully_filled
        and report.get("strategy_fully_filled_order_complete") is not True
    ):
        raise VerifiedFillLedgerHalted(
            "ORDER_NOT_FULLY_FILLED_STRATEGY_COMPATIBLE"
        )
    if not isinstance(order, dict) or not isinstance(fills, list):
        raise VerifiedFillLedgerHalted("ORDER_FILL_INPUT_INVALID")

    position_id = str(order.get("positionId") or "")
    symbol = order.get("symbol")
    side_code = _integer(order.get("side"))
    state = _integer(order.get("state"))
    deal_volume = _decimal(order.get("dealVol"), positive=True)
    average_price = _decimal(order.get("dealAvgPrice"), positive=True)
    fee_currency = order.get("feeCurrency")

    if (
        not position_id
        or not isinstance(symbol, str)
        or not symbol
        or side_code not in SIDE_LABELS
        or state not in {3, 4, 5}
        or fee_currency != "USDT"
    ):
        raise VerifiedFillLedgerHalted(
            "ORDER_FILL_SET_STRATEGY_CONTRACT_MISMATCH"
        )

    parsed: list[VerifiedFill] = []
    for row in fills:
        if not isinstance(row, dict):
            raise VerifiedFillLedgerHalted("FILL_ROW_INVALID")
        category = _integer(row.get("category"))
        if category != 1:
            raise VerifiedFillLedgerHalted(
                "NONSTANDARD_EXECUTION_CATEGORY_REQUIRES_REVIEW"
            )
        trade_id = str(row.get("id") or "")
        timestamp = _integer(row.get("timestamp"))
        if not trade_id or timestamp <= 0:
            raise VerifiedFillLedgerHalted("FILL_IDENTITY_INVALID")
        parsed.append(VerifiedFill(
            trade_id=trade_id,
            order_id=order_id,
            position_id=position_id,
            side=SIDE_LABELS[side_code],
            contracts=_decimal(row.get("vol"), positive=True),
            price=_decimal(row.get("price"), positive=True),
            fee_usdt=_decimal(row.get("fee")),
            timestamp_ms=timestamp,
            category=category,
        ))

    ordered = tuple(sorted(
        parsed,
        key=lambda x: (x.timestamp_ms, x.trade_id),
    ))
    payload = [
        {
            "trade_id": x.trade_id,
            "order_id": x.order_id,
            "position_id": x.position_id,
            "side": x.side,
            "contracts": str(x.contracts),
            "price": str(x.price),
            "fee_usdt": str(x.fee_usdt),
            "timestamp_ms": x.timestamp_ms,
            "category": x.category,
        }
        for x in ordered
    ]
    digest = hashlib.sha256(
        json.dumps(
            payload,
            sort_keys=True,
            separators=(",", ":"),
        ).encode()
    ).hexdigest()

    return VerifiedOrderFillSet(
        order_id=order_id,
        position_id=position_id,
        symbol=symbol,
        side=SIDE_LABELS[side_code],
        order_state=state,
        deal_volume=deal_volume,
        average_price=average_price,
        fee_currency=fee_currency,
        fills=ordered,
        fill_digest_sha256=digest,
        final_order_fill_history_complete=True,
        strategy_fully_filled_order_complete=(
            report.get("strategy_fully_filled_order_complete") is True
        ),
    )


class PostgresVerifiedFillLedger:
    """Append-only transactionally persisted verified order fill sets."""

    def __init__(self, pool: Any):
        self.pool = pool

    async def persist(self, fill_set: VerifiedOrderFillSet) -> str:
        if not isinstance(fill_set, VerifiedOrderFillSet):
            raise VerifiedFillLedgerHalted("VERIFIED_FILL_SET_INVALID")
        try:
            async with self.pool.acquire() as conn:
                async with conn.transaction():
                    existing = await conn.fetchrow(
                        """SELECT fill_digest_sha256, fill_count
                           FROM t100_live_order_fill_sets
                           WHERE order_id=$1""",
                        fill_set.order_id,
                    )
                    if existing is not None:
                        if (
                            str(existing["fill_digest_sha256"])
                            != fill_set.fill_digest_sha256
                            or int(existing["fill_count"])
                            != len(fill_set.fills)
                        ):
                            raise VerifiedFillLedgerHalted(
                                "VERIFIED_FILL_SET_IMMUTABILITY_MISMATCH"
                            )
                        return "ALREADY_VERIFIED"

                    row = await conn.fetchval(
                        """INSERT INTO t100_live_order_fill_sets
                           (order_id,position_id,symbol,side,order_state,
                            deal_volume,average_price,fee_currency,fill_count,
                            fill_digest_sha256,
                            strategy_fully_filled_order_complete)
                           VALUES($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11)
                           RETURNING order_id""",
                        fill_set.order_id,
                        fill_set.position_id,
                        fill_set.symbol,
                        fill_set.side,
                        fill_set.order_state,
                        fill_set.deal_volume,
                        fill_set.average_price,
                        fill_set.fee_currency,
                        len(fill_set.fills),
                        fill_set.fill_digest_sha256,
                        fill_set.strategy_fully_filled_order_complete,
                    )
                    if row is None:
                        raise VerifiedFillLedgerHalted(
                            "VERIFIED_FILL_SET_INSERT_FAILED"
                        )

                    for fill in fill_set.fills:
                        inserted = await conn.fetchval(
                            """INSERT INTO t100_live_order_fills
                               (trade_id,order_id,position_id,side,contracts,
                                price,fee_usdt,timestamp_ms,category)
                               VALUES($1,$2,$3,$4,$5,$6,$7,$8,$9)
                               RETURNING trade_id""",
                            fill.trade_id,
                            fill.order_id,
                            fill.position_id,
                            fill.side,
                            fill.contracts,
                            fill.price,
                            fill.fee_usdt,
                            fill.timestamp_ms,
                            fill.category,
                        )
                        if inserted is None:
                            raise VerifiedFillLedgerHalted(
                                "VERIFIED_FILL_INSERT_FAILED"
                            )
                    return "PERSISTED"
        except VerifiedFillLedgerHalted:
            raise
        except Exception as exc:
            raise VerifiedFillLedgerHalted(
                "VERIFIED_FILL_TRANSACTION_FAILED"
            ) from exc
