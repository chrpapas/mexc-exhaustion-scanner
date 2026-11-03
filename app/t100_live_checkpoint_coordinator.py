"""Persistence-only coordinator for durable T100 checkpoints.

No exchange mutations. It consumes already-read evidence and writes durable
checkpoint state only when the caller explicitly supplies fresh, reconciled,
complete inventory evidence.

Current real MEXC discovery does not yet establish account-wide completeness,
so this coordinator remains unusable for live persistence until that separate
gate is resolved.
"""
from __future__ import annotations

from typing import Any, Protocol

from app.t100_live_checkpoint_journal import (
    CLOSE_VERIFIED,
    CLOSING_LATCHED,
    REVIEW_LATCHED,
    STOP_VERIFIED,
    CheckpointJournalHalted,
    build_initial_checkpoint,
    build_lifecycle_checkpoint,
    build_verified_stop_checkpoint,
)
from app.t100_live_durable_recovery import PositionCheckpoint
from app.t100_live_position_accounting import (
    VerifiedClosedPositionAccounting,
)


class CheckpointCoordinatorHalted(RuntimeError):
    pass


class CheckpointWriter(Protocol):
    async def create_initial(self, current: PositionCheckpoint) -> None: ...
    async def advance(
        self,
        previous: PositionCheckpoint,
        current: PositionCheckpoint,
        *,
        reason: str,
    ) -> None: ...


def _require_evidence_gate(
    *,
    evidence_fresh: bool,
    account_reconciled: bool,
    positions_complete: bool,
    stops_complete: bool,
) -> None:
    if type(evidence_fresh) is not bool or evidence_fresh is not True:
        raise CheckpointCoordinatorHalted("CHECKPOINT_EVIDENCE_NOT_FRESH")
    if type(account_reconciled) is not bool or account_reconciled is not True:
        raise CheckpointCoordinatorHalted("CHECKPOINT_ACCOUNT_NOT_RECONCILED")
    if type(positions_complete) is not bool or positions_complete is not True:
        raise CheckpointCoordinatorHalted("CHECKPOINT_POSITIONS_INCOMPLETE")
    if type(stops_complete) is not bool or stops_complete is not True:
        raise CheckpointCoordinatorHalted("CHECKPOINT_STOPS_INCOMPLETE")


class CheckpointPersistenceCoordinator:
    def __init__(self, writer: CheckpointWriter):
        self.writer = writer

    async def persist_initial_protected(
        self,
        *,
        intent: dict[str, Any],
        order: dict[str, Any],
        positions: Any,
        stops: Any,
        evidence_fresh: bool,
        pre_entry_account_reconciled: bool,
        positions_complete: bool,
        stops_complete: bool,
    ) -> PositionCheckpoint:
        if type(evidence_fresh) is not bool or evidence_fresh is not True:
            raise CheckpointCoordinatorHalted(
                "CHECKPOINT_EVIDENCE_NOT_FRESH"
            )
        if (
            type(pre_entry_account_reconciled) is not bool
            or pre_entry_account_reconciled is not True
        ):
            raise CheckpointCoordinatorHalted(
                "CHECKPOINT_PRE_ENTRY_ACCOUNT_NOT_RECONCILED"
            )
        if type(positions_complete) is not bool or positions_complete is not True:
            raise CheckpointCoordinatorHalted(
                "CHECKPOINT_POSITIONS_INCOMPLETE"
            )
        if type(stops_complete) is not bool or stops_complete is not True:
            raise CheckpointCoordinatorHalted(
                "CHECKPOINT_STOPS_INCOMPLETE"
            )
        if (
            not isinstance(positions, list)
            or not isinstance(stops, list)
            or any(not isinstance(row, dict) for row in positions)
            or any(not isinstance(row, dict) for row in stops)
        ):
            raise CheckpointCoordinatorHalted(
                "CHECKPOINT_INVENTORY_RESPONSE_AMBIGUOUS"
            )

        position_id = str(order.get("positionId") or "")
        symbol = str(order.get("symbol") or "")
        if not position_id or not symbol:
            raise CheckpointCoordinatorHalted(
                "CHECKPOINT_ORDER_IDENTITY_MISSING"
            )

        matching_positions = [
            row for row in positions
            if str(row.get("positionId") or row.get("id") or "") == position_id
            and row.get("symbol") == symbol
        ]
        matching_stops = [
            row for row in stops
            if str(row.get("positionId") or "") == position_id
        ]
        if len(matching_positions) != 1:
            raise CheckpointCoordinatorHalted(
                "CHECKPOINT_POSITION_NOT_UNIQUE"
            )
        if len(matching_stops) != 1:
            raise CheckpointCoordinatorHalted(
                "CHECKPOINT_STOP_NOT_UNIQUE"
            )

        try:
            current = build_initial_checkpoint(
                intent=intent,
                order=order,
                position=matching_positions[0],
                stop=matching_stops[0],
            )
            await self.writer.create_initial(current)
        except CheckpointJournalHalted as exc:
            raise CheckpointCoordinatorHalted(
                f"CHECKPOINT_INITIAL_PERSISTENCE_BLOCKED:{exc}"
            ) from exc
        return current

    async def persist_verified_stop(
        self,
        previous: PositionCheckpoint,
        *,
        amendment: dict[str, Any],
        stops: Any,
        best_profit_pct: Any,
        armed: bool,
        evidence_fresh: bool,
        account_reconciled: bool,
        positions_complete: bool,
        stops_complete: bool,
    ) -> PositionCheckpoint:
        _require_evidence_gate(
            evidence_fresh=evidence_fresh,
            account_reconciled=account_reconciled,
            positions_complete=positions_complete,
            stops_complete=stops_complete,
        )
        if (
            not isinstance(stops, list)
            or any(not isinstance(row, dict) for row in stops)
        ):
            raise CheckpointCoordinatorHalted(
                "CHECKPOINT_STOP_RESPONSE_AMBIGUOUS"
            )
        verified_stop_order_id = str(
            amendment.get("verified_stop_order_id") or ""
        )
        if not verified_stop_order_id:
            raise CheckpointCoordinatorHalted(
                "CHECKPOINT_VERIFIED_STOP_ID_MISSING"
            )
        matching = [
            row for row in stops
            if str(row.get("positionId") or "") == previous.position_id
            and str(row.get("id") or row.get("stopPlanOrderId") or "")
            == verified_stop_order_id
        ]
        if len(matching) != 1:
            raise CheckpointCoordinatorHalted(
                "CHECKPOINT_VERIFIED_STOP_NOT_UNIQUE"
            )

        try:
            current = build_verified_stop_checkpoint(
                previous,
                amendment=amendment,
                stop=matching[0],
                best_profit_pct=best_profit_pct,
                armed=armed,
            )
            await self.writer.advance(
                previous, current, reason=STOP_VERIFIED
            )
        except CheckpointJournalHalted as exc:
            raise CheckpointCoordinatorHalted(
                f"CHECKPOINT_STOP_PERSISTENCE_BLOCKED:{exc}"
            ) from exc
        return current

    async def latch_review(
        self,
        previous: PositionCheckpoint,
    ) -> PositionCheckpoint:
        try:
            current = build_lifecycle_checkpoint(
                previous, target="REVIEW"
            )
            await self.writer.advance(
                previous, current, reason=REVIEW_LATCHED
            )
        except CheckpointJournalHalted as exc:
            raise CheckpointCoordinatorHalted(
                f"CHECKPOINT_REVIEW_PERSISTENCE_BLOCKED:{exc}"
            ) from exc
        return current

    async def latch_closing(
        self,
        previous: PositionCheckpoint,
    ) -> PositionCheckpoint:
        """Durably block autonomous restart before any external close mutation."""
        try:
            current = build_lifecycle_checkpoint(
                previous, target="CLOSING"
            )
            await self.writer.advance(
                previous, current, reason=CLOSING_LATCHED
            )
        except CheckpointJournalHalted as exc:
            raise CheckpointCoordinatorHalted(
                f"CHECKPOINT_CLOSING_PERSISTENCE_BLOCKED:{exc}"
            ) from exc
        return current

    async def persist_verified_closed(
        self,
        previous: PositionCheckpoint,
        *,
        positions: Any,
        stops: Any,
        final_accounting: VerifiedClosedPositionAccounting,
        evidence_fresh: bool,
        account_reconciled: bool,
        positions_complete: bool,
        stops_complete: bool,
    ) -> PositionCheckpoint:
        _require_evidence_gate(
            evidence_fresh=evidence_fresh,
            account_reconciled=account_reconciled,
            positions_complete=positions_complete,
            stops_complete=stops_complete,
        )
        if (
            previous.lifecycle_state != "CLOSING"
            or not isinstance(positions, list)
            or not isinstance(stops, list)
            or any(not isinstance(row, dict) for row in positions)
            or any(not isinstance(row, dict) for row in stops)
        ):
            raise CheckpointCoordinatorHalted(
                "CHECKPOINT_CLOSE_EVIDENCE_AMBIGUOUS"
            )

        position_present = any(
            str(row.get("positionId") or row.get("id") or "")
            == previous.position_id
            for row in positions
        )
        stop_present = any(
            str(row.get("positionId") or "") == previous.position_id
            and str(row.get("id") or row.get("stopPlanOrderId") or "")
            == previous.stop_order_id
            for row in stops
        )
        if position_present:
            raise CheckpointCoordinatorHalted(
                "CHECKPOINT_CLOSE_POSITION_STILL_PRESENT"
            )
        if stop_present:
            raise CheckpointCoordinatorHalted(
                "CHECKPOINT_CLOSE_STOP_STILL_PRESENT"
            )
        if not isinstance(
            final_accounting,
            VerifiedClosedPositionAccounting,
        ):
            raise CheckpointCoordinatorHalted(
                "CHECKPOINT_FINAL_ACCOUNTING_UNVERIFIED"
            )
        reconciled = final_accounting.reconciliation
        if (
            final_accounting.close_provenance_verified is not True
            or final_accounting.fill_history_complete is not True
            or final_accounting.funding_history_complete is not True
            or reconciled.status != "CLOSED_RECONCILED"
            or reconciled.position_id != previous.position_id
            or reconciled.opened_contracts != previous.contracts
            or reconciled.closed_contracts != previous.contracts
            or reconciled.remaining_contracts != 0
            or final_accounting.entry_order_id
            != previous.exchange_order_id
            or final_accounting.stop_order_id
            != previous.stop_order_id
            or not isinstance(final_accounting.close_order_id, str)
            or not final_accounting.close_order_id
        ):
            raise CheckpointCoordinatorHalted(
                "CHECKPOINT_FINAL_ACCOUNTING_PROVENANCE_MISMATCH"
            )

        try:
            current = build_lifecycle_checkpoint(
                previous,
                target="CLOSED",
                position_absent=True,
                stop_absent=True,
                close_provenance_verified=True,
            )
            await self.writer.advance(
                previous, current, reason=CLOSE_VERIFIED
            )
        except CheckpointJournalHalted as exc:
            raise CheckpointCoordinatorHalted(
                f"CHECKPOINT_CLOSE_PERSISTENCE_BLOCKED:{exc}"
            ) from exc
        return current
