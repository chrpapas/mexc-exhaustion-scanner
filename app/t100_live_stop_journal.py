"""Durable stop-amendment intent planning and read-only reconciliation.

This module NEVER modifies a stop. Persisting an intent is not permission
to POST. Unknown exchange outcomes must not be blindly retried.
"""
from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
import hashlib
from typing import Any, Protocol


class StopJournalHalted(RuntimeError):
    pass


@dataclass(frozen=True)
class StopAmendment:
    external_id: str
    symbol: str
    position_id: str
    stop_order_id: str
    previous_stop: Decimal
    proposed_stop: Decimal

    @classmethod
    def build(cls, *, symbol: str, position_id: str, stop_order_id: str,
              previous_stop: Decimal, proposed_stop: Decimal) -> "StopAmendment":
        if not symbol or not position_id or not stop_order_id:
            raise ValueError("STOP_IDENTITY_MISSING")
        for n in (previous_stop, proposed_stop):
            if not n.is_finite() or n <= 0:
                raise ValueError("INVALID_STOP_PRICE")
        if proposed_stop >= previous_stop:
            raise ValueError("SHORT_STOP_MUST_TIGHTEN")
        seed = f"{symbol}:{position_id}:{stop_order_id}:{previous_stop}:{proposed_stop}"
        oid = "t100s" + hashlib.sha256(seed.encode()).hexdigest()[:24]
        return cls(oid, symbol, position_id, stop_order_id, previous_stop, proposed_stop)


class StopJournal(Protocol):
    async def prepare(self, amendment: StopAmendment) -> bool: ...
    async def state(self, external_id: str) -> str: ...
    async def transition(self, external_id: str, expected: str, target: str) -> bool: ...
    async def mark_verified(
        self,
        external_id: str,
        expected: str,
        verified_stop_order_id: str,
    ) -> bool: ...


class StopReader(Protocol):
    async def open_stop_orders(self, symbol: str | None = None) -> list[dict[str, Any]]: ...


class PostgresStopJournal:
    def __init__(self, pool: Any):
        self.pool = pool

    async def prepare(self, amendment: StopAmendment) -> bool:
        row = await self.pool.fetchval(
            """INSERT INTO t100_live_stop_amendments
               (external_id, symbol, position_id, stop_order_id, previous_stop, proposed_stop)
               VALUES($1,$2,$3,$4,$5,$6)
               ON CONFLICT DO NOTHING RETURNING id""",
            amendment.external_id, amendment.symbol, amendment.position_id,
            amendment.stop_order_id, amendment.previous_stop, amendment.proposed_stop,
        )
        return row is not None

    async def state(self, external_id: str) -> str:
        result = await self.pool.fetchval(
            "SELECT state FROM t100_live_stop_amendments WHERE external_id=$1", external_id
        )
        if result is None:
            raise StopJournalHalted("STOP_AMENDMENT_NOT_FOUND")
        return str(result)

    async def transition(self, external_id: str, expected: str, target: str) -> bool:
        row = await self.pool.fetchval(
            """UPDATE t100_live_stop_amendments SET state=$3, updated_at=now()
               WHERE external_id=$1 AND state=$2 RETURNING id""",
            external_id, expected, target,
        )
        return row is not None

    async def mark_verified(
        self,
        external_id: str,
        expected: str,
        verified_stop_order_id: str,
    ) -> bool:
        if not isinstance(verified_stop_order_id, str) or not verified_stop_order_id:
            raise StopJournalHalted("VERIFIED_STOP_ID_MISSING")
        row = await self.pool.fetchval(
            """UPDATE t100_live_stop_amendments
               SET state='VERIFIED',
                   verified_stop_order_id=$3,
                   updated_at=now()
               WHERE external_id=$1 AND state=$2
               RETURNING id""",
            external_id,
            expected,
            verified_stop_order_id,
        )
        return row is not None


class ReadOnlyStopReconciler:
    def __init__(self, journal: StopJournal, exchange: StopReader):
        self.journal = journal
        self.exchange = exchange

    async def verify(self, amendment: StopAmendment) -> str:
        """Read-only. Uncertain results remain UNKNOWN and prohibit retries."""
        state = await self.journal.state(amendment.external_id)
        if state not in {"PREPARED", "UNKNOWN", "SUBMITTING", "VERIFIED"}:
            raise StopJournalHalted("INVALID_STOP_JOURNAL_STATE")
        try:
            orders = await self.exchange.open_stop_orders(amendment.symbol)
        except Exception as exc:
            raise StopJournalHalted("STOP_EXCHANGE_READ_FAILED") from exc
        if not isinstance(orders, list) or any(not isinstance(o, dict) for o in orders):
            raise StopJournalHalted("STOP_EXCHANGE_RESPONSE_AMBIGUOUS")
        matching = [
            o for o in orders
            if str(o.get("positionId")) == amendment.position_id
            and str(o.get("id") or o.get("stopPlanOrderId")) == amendment.stop_order_id
        ]
        if len(matching) != 1:
            raise StopJournalHalted("STOP_NOT_UNIQUELY_FOUND")
        order = matching[0]
        try:
            price = Decimal(str(order["stopLossPrice"]))
            active = int(order["state"]) == 1
        except (KeyError, TypeError, ValueError, InvalidOperation) as exc:
            raise StopJournalHalted("INVALID_NATIVE_STOP_STATE") from exc
        if not price.is_finite() or price <= 0 or not active:
            raise StopJournalHalted("INVALID_NATIVE_STOP_STATE")
        if price > amendment.previous_stop:
            raise StopJournalHalted("SHORT_STOP_LOOSENED")
        if price > amendment.proposed_stop:
            # A prepared-but-unapplied amendment is not a failed order
            # and is never authorization to send/retry.
            raise StopJournalHalted("STOP_NOT_TIGHTENED_UNRESOLVED")
        if state != "VERIFIED":
            if not await self.journal.mark_verified(
                amendment.external_id,
                state,
                amendment.stop_order_id,
            ):
                raise StopJournalHalted("STOP_JOURNAL_CAS_FAILURE")
        return "VERIFIED_READ_ONLY"
