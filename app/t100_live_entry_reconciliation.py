"""GET/persist-only restart-safe protected-entry reconciliation.

This module DOES NOT submit an entry order. It resumes only an already
persisted ACKed intent and binds that durable intent to:
- the exact exchange order resolved by externalOid,
- the complete immutable order-scoped fill set,
- the complete current position / native TP-SL inventory,
- a version-1 durable protected-position checkpoint.

If fill persistence succeeds but checkpoint persistence fails, rerunning this
pipeline is safe: the fill ledger is immutable/idempotent and no order POST is
ever retried here.
"""
from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from typing import Any, Protocol

from app.t100_live_active_tpsl import assess_active_tpsl_semantics
from app.t100_live_checkpoint_coordinator import (
    CheckpointCoordinatorHalted,
    CheckpointPersistenceCoordinator,
)
from app.t100_live_durable_recovery import PositionCheckpoint
from app.t100_live_fill_ledger import (
    VerifiedFillLedgerHalted,
    VerifiedFillStore,
    VerifiedOrderFillSet,
    build_verified_order_fill_set,
)


class ProtectedEntryReconciliationHalted(RuntimeError):
    pass


class DurableIntentReader(Protocol):
    async def record(self, oid: str) -> dict[str, Any]: ...


class EntryEvidenceExchange(Protocol):
    async def order_by_external_oid(
        self,
        symbol: str,
        external_oid: str,
    ) -> dict[str, Any] | None: ...

    async def order_deal_details(
        self,
        order_id: str,
    ) -> list[dict[str, Any]]: ...


@dataclass(frozen=True)
class ProtectedEntryReconciliation:
    checkpoint: PositionCheckpoint
    fill_set: VerifiedOrderFillSet
    fill_persistence_status: str
    protection_semantics_verified: bool = True
    live_authorization: bool = False


def _positive_decimal(raw: Any, field: str) -> Decimal:
    try:
        value = Decimal(str(raw))
    except (InvalidOperation, TypeError, ValueError) as exc:
        raise ProtectedEntryReconciliationHalted(
            f"INVALID_{field}"
        ) from exc
    if not value.is_finite() or value <= 0:
        raise ProtectedEntryReconciliationHalted(f"INVALID_{field}")
    return value


class ProtectedEntryReconciliationPipeline:
    def __init__(
        self,
        *,
        intents: DurableIntentReader,
        exchange: EntryEvidenceExchange,
        fills: VerifiedFillStore,
        checkpoints: CheckpointPersistenceCoordinator,
    ) -> None:
        self.intents = intents
        self.exchange = exchange
        self.fills = fills
        self.checkpoints = checkpoints

    async def reconcile_and_persist(
        self,
        *,
        external_oid: str,
        positions: Any,
        stops: Any,
        evidence_fresh: bool,
        pre_entry_account_reconciled: bool,
        positions_complete: bool,
        stops_complete: bool,
    ) -> ProtectedEntryReconciliation:
        if not isinstance(external_oid, str) or not external_oid:
            raise ProtectedEntryReconciliationHalted(
                "ENTRY_EXTERNAL_OID_INVALID"
            )
        if (
            not isinstance(positions, list)
            or not isinstance(stops, list)
            or any(not isinstance(row, dict) for row in positions)
            or any(not isinstance(row, dict) for row in stops)
        ):
            raise ProtectedEntryReconciliationHalted(
                "ENTRY_INVENTORY_RESPONSE_AMBIGUOUS"
            )

        try:
            intent = await self.intents.record(external_oid)
        except Exception as exc:
            raise ProtectedEntryReconciliationHalted(
                "ENTRY_INTENT_READ_FAILED"
            ) from exc
        if not isinstance(intent, dict):
            raise ProtectedEntryReconciliationHalted(
                "ENTRY_INTENT_RECORD_INVALID"
            )

        durable_oid = intent.get("external_oid")
        symbol = intent.get("symbol")
        order_id = str(intent.get("exchange_order_id") or "")
        if (
            durable_oid != external_oid
            or intent.get("state") != "ACK"
            or not isinstance(symbol, str)
            or not symbol
            or not order_id.isdigit()
            or int(order_id) <= 0
        ):
            raise ProtectedEntryReconciliationHalted(
                "ENTRY_INTENT_NOT_DURABLY_ACKED"
            )
        contracts = _positive_decimal(
            intent.get("volume_contracts"),
            "ENTRY_INTENT_VOLUME",
        )
        _positive_decimal(
            intent.get("stop_price"),
            "ENTRY_INTENT_STOP_PRICE",
        )

        try:
            order = await self.exchange.order_by_external_oid(
                symbol,
                external_oid,
            )
        except Exception as exc:
            raise ProtectedEntryReconciliationHalted(
                "ENTRY_ORDER_LOOKUP_FAILED"
            ) from exc
        if not isinstance(order, dict):
            raise ProtectedEntryReconciliationHalted(
                "ENTRY_ORDER_UNRESOLVED_NO_RESUBMIT"
            )

        if (
            str(order.get("orderId") or "") != order_id
            or order.get("externalOid") != external_oid
            or order.get("symbol") != symbol
            or order.get("side") != 3
            or order.get("state") != 3
            or _positive_decimal(
                order.get("dealVol"),
                "ENTRY_ORDER_DEAL_VOLUME",
            ) != contracts
        ):
            raise ProtectedEntryReconciliationHalted(
                "ENTRY_ORDER_DURABLE_IDENTITY_MISMATCH"
            )

        try:
            raw_fills = await self.exchange.order_deal_details(order_id)
            fill_set = build_verified_order_fill_set(
                order_id=order_id,
                order=order,
                fills=raw_fills,
                require_strategy_fully_filled=True,
            )
        except VerifiedFillLedgerHalted as exc:
            raise ProtectedEntryReconciliationHalted(
                f"ENTRY_FILL_RECONCILIATION_BLOCKED:{exc}"
            ) from exc
        except Exception as exc:
            raise ProtectedEntryReconciliationHalted(
                "ENTRY_FILL_READ_FAILED"
            ) from exc

        position_id = str(order.get("positionId") or "")
        if (
            fill_set.side != "OPEN_SHORT"
            or fill_set.order_id != order_id
            or fill_set.position_id != position_id
            or fill_set.symbol != symbol
            or fill_set.deal_volume != contracts
        ):
            raise ProtectedEntryReconciliationHalted(
                "ENTRY_FILL_PROVENANCE_MISMATCH"
            )

        semantics = assess_active_tpsl_semantics(positions, stops)
        if semantics.get("active_tpsl_row_semantics_verified") is not True:
            raise ProtectedEntryReconciliationHalted(
                "ENTRY_NATIVE_PROTECTION_SEMANTICS_UNVERIFIED"
            )

        matching_positions = [
            row for row in positions
            if str(row.get("positionId") or row.get("id") or "")
            == position_id
            and row.get("symbol") == symbol
        ]
        matching_stops = [
            row for row in stops
            if str(row.get("positionId") or "") == position_id
            and row.get("symbol") in (None, symbol)
        ]
        if len(matching_positions) != 1 or len(matching_stops) != 1:
            raise ProtectedEntryReconciliationHalted(
                "ENTRY_PROTECTED_INVENTORY_NOT_UNIQUE"
            )

        try:
            fill_status = await self.fills.persist(fill_set)
        except Exception as exc:
            raise ProtectedEntryReconciliationHalted(
                "ENTRY_FILL_PERSISTENCE_FAILED"
            ) from exc
        if fill_status not in {"PERSISTED", "ALREADY_VERIFIED"}:
            raise ProtectedEntryReconciliationHalted(
                "ENTRY_FILL_PERSISTENCE_STATUS_INVALID"
            )

        checkpoint_intent = {
            "symbol": symbol,
            "external_oid": external_oid,
            "exchange_order_id": order_id,
            "volume_contracts": contracts,
            "stop_price": intent.get("stop_price"),
            "state": "ACK",
        }
        try:
            checkpoint = await self.checkpoints.persist_initial_protected(
                intent=checkpoint_intent,
                order=order,
                positions=positions,
                stops=stops,
                evidence_fresh=evidence_fresh,
                pre_entry_account_reconciled=pre_entry_account_reconciled,
                positions_complete=positions_complete,
                stops_complete=stops_complete,
            )
        except CheckpointCoordinatorHalted as exc:
            raise ProtectedEntryReconciliationHalted(
                f"ENTRY_CHECKPOINT_PERSISTENCE_BLOCKED:{exc}"
            ) from exc

        if (
            checkpoint.exchange_order_id != fill_set.order_id
            or checkpoint.position_id != fill_set.position_id
            or checkpoint.contracts != fill_set.deal_volume
            or checkpoint.symbol != fill_set.symbol
        ):
            raise ProtectedEntryReconciliationHalted(
                "ENTRY_CHECKPOINT_FILL_PROVENANCE_MISMATCH"
            )

        return ProtectedEntryReconciliation(
            checkpoint=checkpoint,
            fill_set=fill_set,
            fill_persistence_status=fill_status,
        )
