"""Read-only restart gate for T100 experimental live components.

Checks persisted order intents and full exchange inventory before allowing
a SHADOW_ONLY process to start. Never authorizes trading or makes mutations.
A database read error or any non-flat state requires manual reconciliation.
"""
from __future__ import annotations

from dataclasses import dataclass
from collections.abc import Mapping
from typing import Any, Protocol


class RecoveryHalted(RuntimeError):
    pass


class LedgerReader(Protocol):
    async def fetch(self, query: str) -> list[Any]: ...


class ExchangeReader(Protocol):
    async def open_positions(self, symbol: str | None = None) -> list[dict]: ...
    async def open_stop_orders(self, symbol: str | None = None) -> list[dict]: ...


@dataclass(frozen=True)
class RecoveryReport:
    mode: str
    persisted_intents: int
    exchange_positions: int
    exchange_stops: int
    reason: str


class RestartRecoveryGate:
    def __init__(self, ledger: LedgerReader, exchange: ExchangeReader):
        self.ledger = ledger
        self.exchange = exchange

    async def inspect(self) -> RecoveryReport:
        # A flat ledger is necessary, not sufficient, for live readiness.
        # Any historical intent requires an explicit operator-approved
        # reconciliation procedure that is not implemented here.
        try:
            intents = await self.ledger.fetch(
                "SELECT external_oid, state, exchange_order_id "
                "FROM t100_live_order_intents ORDER BY id"
            )
            stop_amendments = await self.ledger.fetch(
                "SELECT external_id, state FROM t100_live_stop_amendments "
                "WHERE state <> 'VERIFIED' ORDER BY id"
            )
        except Exception as exc:
            raise RecoveryHalted("LEDGER_UNAVAILABLE") from exc
        if not isinstance(intents, list) or not isinstance(stop_amendments, list):
            raise RecoveryHalted("LEDGER_RESPONSE_AMBIGUOUS")
        if intents:
            if not all(
                isinstance(row, Mapping)
                and row.get("external_oid")
                and row.get("state")
                for row in intents
            ):
                raise RecoveryHalted("LEDGER_ROWS_INVALID")
            raise RecoveryHalted("PERSISTED_INTENTS_REQUIRE_MANUAL_RECONCILIATION")
        if stop_amendments:
            raise RecoveryHalted("UNRESOLVED_STOP_AMENDMENTS_REQUIRE_MANUAL_RECONCILIATION")
        try:
            positions = await self.exchange.open_positions()
            stops = await self.exchange.open_stop_orders()
        except Exception as exc:
            raise RecoveryHalted("EXCHANGE_READ_UNAVAILABLE") from exc
        if not isinstance(positions, list) or not isinstance(stops, list):
            raise RecoveryHalted("EXCHANGE_RESPONSE_AMBIGUOUS")
        if any(not isinstance(item, dict) for item in positions + stops):
            raise RecoveryHalted("EXCHANGE_INVENTORY_MALFORMED")
        if positions or stops:
            raise RecoveryHalted("EXCHANGE_NOT_FLAT_REQUIRES_MANUAL_RECONCILIATION")
        return RecoveryReport(
            mode="SHADOW_ONLY",
            persisted_intents=0,
            exchange_positions=0,
            exchange_stops=0,
            reason="FLAT_READ_ONLY_VERIFIED_NOT_LIVE_AUTHORIZATION",
        )
