"""No-POST durable pipeline for verified T100 stop updates.

This module composes:
- durable stop-amendment journal coordination,
- read-only exchange outcome reconciliation,
- durable position checkpoint persistence.

It contains no exchange mutation method and cannot authorize live trading.
If the amendment becomes VERIFIED but checkpoint persistence fails, restart
must remain fail-closed; the same verified current exchange outcome can then
be reconciled again and the checkpoint write retried without re-sending the
exchange mutation.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from app.t100_live_checkpoint_coordinator import (
    CheckpointCoordinatorHalted,
    CheckpointPersistenceCoordinator,
)
from app.t100_live_durable_recovery import PositionCheckpoint
from app.t100_live_stop_amendment_coordinator import (
    StopAmendmentLifecycleCoordinator,
    StopReconciliationOutcome,
)
from app.t100_live_stop_journal import StopAmendment


class StopUpdatePipelineHalted(RuntimeError):
    pass


@dataclass(frozen=True)
class StopUpdatePersistenceOutcome:
    checkpoint: PositionCheckpoint
    reconciliation: StopReconciliationOutcome
    live_authorization: bool = False


class StopUpdatePersistencePipeline:
    def __init__(
        self,
        amendment_coordinator: StopAmendmentLifecycleCoordinator,
        checkpoint_coordinator: CheckpointPersistenceCoordinator,
    ) -> None:
        self.amendments = amendment_coordinator
        self.checkpoints = checkpoint_coordinator

    @staticmethod
    def _verified_amendment_row(
        amendment: StopAmendment,
        reconciliation: StopReconciliationOutcome,
    ) -> dict[str, Any]:
        return {
            "state": "VERIFIED",
            "external_id": amendment.external_id,
            "symbol": amendment.symbol,
            "position_id": amendment.position_id,
            "stop_order_id": amendment.stop_order_id,
            "verified_stop_order_id": (
                reconciliation.verified_stop_order_id
            ),
            "previous_stop": amendment.previous_stop,
            "proposed_stop": amendment.proposed_stop,
        }

    async def reconcile_observed_and_persist(
        self,
        previous: PositionCheckpoint,
        amendment: StopAmendment,
        *,
        before_stops: Any,
        after_stops: Any,
        finished_stops: Any | None = None,
        best_profit_pct: Any,
        armed: bool,
        evidence_fresh: bool,
        account_reconciled: bool,
        positions_complete: bool,
        stops_complete: bool,
    ) -> StopUpdatePersistenceOutcome:
        reconciliation = (
            await self.amendments.reconcile_observed_transition(
                amendment,
                before_stops=before_stops,
                after_stops=after_stops,
                finished_stops=finished_stops,
            )
        )
        return await self._persist(
            previous,
            amendment,
            reconciliation,
            current_stops=after_stops,
            best_profit_pct=best_profit_pct,
            armed=armed,
            evidence_fresh=evidence_fresh,
            account_reconciled=account_reconciled,
            positions_complete=positions_complete,
            stops_complete=stops_complete,
        )

    async def reconcile_current_and_persist(
        self,
        previous: PositionCheckpoint,
        amendment: StopAmendment,
        *,
        current_stops: Any,
        finished_stops: Any | None = None,
        best_profit_pct: Any,
        armed: bool,
        evidence_fresh: bool,
        account_reconciled: bool,
        positions_complete: bool,
        stops_complete: bool,
    ) -> StopUpdatePersistenceOutcome:
        """Restart-safe path for SUBMITTING/UNKNOWN/VERIFIED amendments."""
        reconciliation = (
            await self.amendments.reconcile_current_outcome(
                amendment,
                current_stops=current_stops,
                finished_stops=finished_stops,
            )
        )
        return await self._persist(
            previous,
            amendment,
            reconciliation,
            current_stops=current_stops,
            best_profit_pct=best_profit_pct,
            armed=armed,
            evidence_fresh=evidence_fresh,
            account_reconciled=account_reconciled,
            positions_complete=positions_complete,
            stops_complete=stops_complete,
        )

    async def _persist(
        self,
        previous: PositionCheckpoint,
        amendment: StopAmendment,
        reconciliation: StopReconciliationOutcome,
        *,
        current_stops: Any,
        best_profit_pct: Any,
        armed: bool,
        evidence_fresh: bool,
        account_reconciled: bool,
        positions_complete: bool,
        stops_complete: bool,
    ) -> StopUpdatePersistenceOutcome:
        try:
            checkpoint = await self.checkpoints.persist_verified_stop(
                previous,
                amendment=self._verified_amendment_row(
                    amendment,
                    reconciliation,
                ),
                stops=current_stops,
                best_profit_pct=best_profit_pct,
                armed=armed,
                evidence_fresh=evidence_fresh,
                account_reconciled=account_reconciled,
                positions_complete=positions_complete,
                stops_complete=stops_complete,
            )
        except CheckpointCoordinatorHalted as exc:
            # Deliberately do not roll the amendment journal back from
            # VERIFIED. That unresolved checkpoint mismatch is precisely
            # what blocks restart until this persistence step is recovered.
            raise StopUpdatePipelineHalted(
                f"CHECKPOINT_PERSISTENCE_FAILED:{exc}"
            ) from exc

        return StopUpdatePersistenceOutcome(
            checkpoint=checkpoint,
            reconciliation=reconciliation,
        )
