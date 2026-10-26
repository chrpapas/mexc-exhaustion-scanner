"""Read/persist-only recovery of one protected pilot entry.

This module never submits an order. It is intended for the first tiny pilot
starting from a durably reconciled flat account.

Recovery chain:
- durable intent row must be ACK with an immutable exchange_order_id;
- exchange order is looked up by externalOid and must match that ACK;
- order-scoped fills must be final, complete, and strategy-compatible;
- the verified fill set is persisted append-only;
- post-entry account inventory must contain exactly one expected short
  position and exactly one active native TP/SL row covering it;
- the version-1 durable position checkpoint is persisted.

A missing or unresolved order is never permission to resubmit.
"""
from __future__ import annotations

from dataclasses import dataclass
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
    build_verified_order_fill_set,
)


class EntryRecoveryHalted(RuntimeError):
    pass


class IntentRecordStore(Protocol):
    async def record(self, oid: str) -> dict[str, Any]: ...


class EntryRecoveryExchange(Protocol):
    async def order_by_external_oid(
        self, symbol: str, external_oid: str
    ) -> dict[str, Any] | None: ...

    async def order_deal_details(
        self, order_id: str
    ) -> list[dict[str, Any]]: ...


@dataclass(frozen=True)
class ProtectedEntryRecoveryOutcome:
    checkpoint: PositionCheckpoint
    fill_persistence_status: str
    live_authorization: bool = False


class ProtectedEntryRecoveryPipeline:
    def __init__(
        self,
        *,
        intent_store: IntentRecordStore,
        exchange: EntryRecoveryExchange,
        fill_store: VerifiedFillStore,
        checkpoint_coordinator: CheckpointPersistenceCoordinator,
    ) -> None:
        self.intent_store = intent_store
        self.exchange = exchange
        self.fill_store = fill_store
        self.checkpoints = checkpoint_coordinator

    async def reconcile_and_persist(
        self,
        *,
        external_oid: str,
        positions: Any,
        stops: Any,
        pre_entry_account_reconciled: bool,
        evidence_fresh: bool,
        positions_complete: bool,
        stops_complete: bool,
    ) -> ProtectedEntryRecoveryOutcome:
        if not isinstance(external_oid, str) or not external_oid:
            raise EntryRecoveryHalted("ENTRY_EXTERNAL_OID_INVALID")
        if (
            type(pre_entry_account_reconciled) is not bool
            or pre_entry_account_reconciled is not True
        ):
            raise EntryRecoveryHalted(
                "ENTRY_PRE_ENTRY_ACCOUNT_NOT_RECONCILED"
            )
        if (
            type(evidence_fresh) is not bool
            or type(positions_complete) is not bool
            or type(stops_complete) is not bool
            or evidence_fresh is not True
            or positions_complete is not True
            or stops_complete is not True
        ):
            raise EntryRecoveryHalted(
                "ENTRY_POST_ENTRY_INVENTORY_NOT_COMPLETE_FRESH"
            )
        if (
            not isinstance(positions, list)
            or not isinstance(stops, list)
            or any(not isinstance(row, dict) for row in positions)
            or any(not isinstance(row, dict) for row in stops)
        ):
            raise EntryRecoveryHalted("ENTRY_INVENTORY_AMBIGUOUS")

        try:
            intent = await self.intent_store.record(external_oid)
        except Exception as exc:
            raise EntryRecoveryHalted(
                "ENTRY_INTENT_READ_FAILED"
            ) from exc
        if not isinstance(intent, dict):
            raise EntryRecoveryHalted("ENTRY_INTENT_ROW_INVALID")
        if (
            intent.get("state") != "ACK"
            or str(intent.get("external_oid") or "") != external_oid
            or not str(intent.get("exchange_order_id") or "")
            or not str(intent.get("symbol") or "")
        ):
            raise EntryRecoveryHalted("ENTRY_INTENT_NOT_DURABLY_ACKED")

        symbol = str(intent["symbol"])
        expected_order_id = str(intent["exchange_order_id"])

        try:
            order = await self.exchange.order_by_external_oid(
                symbol, external_oid
            )
        except Exception as exc:
            raise EntryRecoveryHalted(
                "ENTRY_ORDER_LOOKUP_FAILED"
            ) from exc
        if not isinstance(order, dict):
            raise EntryRecoveryHalted(
                "ENTRY_ORDER_UNRESOLVED_NO_RESUBMIT"
            )
        if (
            str(order.get("orderId") or "") != expected_order_id
            or str(order.get("externalOid") or "") != external_oid
            or order.get("symbol") != symbol
        ):
            raise EntryRecoveryHalted("ENTRY_ORDER_IDENTITY_MISMATCH")

        try:
            fills = await self.exchange.order_deal_details(
                expected_order_id
            )
            fill_set = build_verified_order_fill_set(
                order_id=expected_order_id,
                order=order,
                fills=fills,
                require_strategy_fully_filled=True,
            )
        except VerifiedFillLedgerHalted as exc:
            raise EntryRecoveryHalted(
                f"ENTRY_FILL_RECONCILIATION_BLOCKED:{exc}"
            ) from exc
        except Exception as exc:
            raise EntryRecoveryHalted(
                "ENTRY_FILL_READ_FAILED"
            ) from exc

        if (
            fill_set.side != "OPEN_SHORT"
            or fill_set.position_id != str(order.get("positionId") or "")
            or fill_set.symbol != symbol
        ):
            raise EntryRecoveryHalted(
                "ENTRY_FILL_PROVENANCE_MISMATCH"
            )

        # Pilot starts from a certified-flat account. The first protected
        # entry must therefore be the only current position and TP/SL row.
        if len(positions) != 1 or len(stops) != 1:
            raise EntryRecoveryHalted(
                "ENTRY_PILOT_POST_STATE_NOT_SINGLE_PROTECTED_POSITION"
            )

        position_id = fill_set.position_id
        position = positions[0]
        stop = stops[0]
        if (
            str(position.get("positionId") or position.get("id") or "")
            != position_id
            or position.get("symbol") != symbol
            or str(stop.get("positionId") or "") != position_id
        ):
            raise EntryRecoveryHalted(
                "ENTRY_POST_STATE_IDENTITY_MISMATCH"
            )

        tpsl = assess_active_tpsl_semantics(positions, stops)
        if tpsl.get("active_tpsl_row_semantics_verified") is not True:
            raise EntryRecoveryHalted(
                f"ENTRY_NATIVE_PROTECTION_UNVERIFIED:{tpsl.get('status', 'UNKNOWN')}"
            )

        try:
            fill_status = await self.fill_store.persist(fill_set)
        except Exception as exc:
            raise EntryRecoveryHalted(
                "ENTRY_FILL_PERSISTENCE_FAILED"
            ) from exc

        try:
            checkpoint = await self.checkpoints.persist_initial_protected(
                intent=intent,
                order=order,
                positions=positions,
                stops=stops,
                evidence_fresh=True,
                pre_entry_account_reconciled=True,
                positions_complete=True,
                stops_complete=True,
            )
        except CheckpointCoordinatorHalted as exc:
            # Verified fill persistence is append-only/idempotent. If the
            # checkpoint write fails, restart can run this pipeline again
            # without submitting another exchange order.
            raise EntryRecoveryHalted(
                f"ENTRY_CHECKPOINT_PERSISTENCE_FAILED:{exc}"
            ) from exc

        return ProtectedEntryRecoveryOutcome(
            checkpoint=checkpoint,
            fill_persistence_status=fill_status,
        )
