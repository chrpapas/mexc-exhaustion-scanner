"""Exactly-once-attempt stop amendment mutation coordinator.

This is the only intended bridge from a durable StopAmendment journal record
to the MEXC TP/SL mutation transport.

Protocol:
1. require typed live mutation permit
2. durable PREPARED -> SUBMITTING CAS
3. exactly one exchange POST attempt
4. exception => SUBMITTING -> UNKNOWN, never auto-retry
5. success => remain SUBMITTING until independent GET reconciliation marks
   VERIFIED

A second invocation for SUBMITTING/UNKNOWN/VERIFIED is reconciliation-only and
must never emit another POST.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from app.t100_live_authorization import (
    LiveMutationPermit,
    require_live_mutation_permit,
)
from app.t100_live_stop_journal import StopAmendment
from app.t100_live_stop_amendment_coordinator import (
    StopAmendmentCoordinatorHalted,
    StopAmendmentLifecycleCoordinator,
)


class StopMutationExecutionHalted(RuntimeError):
    pass


class StopMutationTransport(Protocol):
    async def submit_native_short_stop_tightening(
        self,
        *,
        stop_order_id: int,
        new_stop_price: str,
        previous_stop_price: str,
        permit: LiveMutationPermit,
    ) -> None: ...


@dataclass(frozen=True)
class StopMutationAttempt:
    external_id: str
    journal_state: str
    post_attempted: bool
    reconciliation_required: bool = True
    live_authorization: bool = False


class StopMutationExecutor:
    def __init__(
        self,
        *,
        lifecycle: StopAmendmentLifecycleCoordinator,
        transport: StopMutationTransport,
    ) -> None:
        self.lifecycle = lifecycle
        self.transport = transport

    async def submit_once(
        self,
        amendment: StopAmendment,
        *,
        permit: LiveMutationPermit,
    ) -> StopMutationAttempt:
        require_live_mutation_permit(permit)
        if not isinstance(amendment, StopAmendment):
            raise StopMutationExecutionHalted(
                "STOP_AMENDMENT_INVALID"
            )

        if (
            not amendment.stop_order_id.isdigit()
            or int(amendment.stop_order_id) <= 0
        ):
            raise StopMutationExecutionHalted(
                "STOP_ORDER_ID_INVALID_BEFORE_SUBMISSION"
            )

        try:
            state = await self.lifecycle.state(amendment)
        except StopAmendmentCoordinatorHalted as exc:
            raise StopMutationExecutionHalted(
                f"STOP_JOURNAL_STATE_UNAVAILABLE:{exc}"
            ) from exc

        if state in {"SUBMITTING", "UNKNOWN", "VERIFIED"}:
            raise StopMutationExecutionHalted(
                f"STOP_MUTATION_ALREADY_STARTED_RECONCILE_ONLY:{state}"
            )
        if state != "PREPARED":
            raise StopMutationExecutionHalted(
                "STOP_MUTATION_JOURNAL_STATE_INVALID"
            )

        try:
            await self.lifecycle.mark_submitting(amendment)
        except StopAmendmentCoordinatorHalted as exc:
            raise StopMutationExecutionHalted(
                f"STOP_SUBMITTING_PERSISTENCE_FAILED:{exc}"
            ) from exc

        try:
            await self.transport.submit_native_short_stop_tightening(
                stop_order_id=int(amendment.stop_order_id),
                previous_stop_price=str(amendment.previous_stop),
                new_stop_price=str(amendment.proposed_stop),
                permit=permit,
            )
        except Exception as exc:
            try:
                await self.lifecycle.mark_unknown(amendment)
            except Exception as journal_exc:
                raise StopMutationExecutionHalted(
                    "STOP_MUTATION_UNKNOWN_AND_JOURNAL_UPDATE_FAILED"
                ) from journal_exc
            raise StopMutationExecutionHalted(
                "STOP_MUTATION_OUTCOME_UNKNOWN_RECONCILE_ONLY"
            ) from exc

        # Deliberately remain SUBMITTING. Successful HTTP response is not
        # sufficient to mark the exchange outcome VERIFIED.
        return StopMutationAttempt(
            external_id=amendment.external_id,
            journal_state="SUBMITTING",
            post_attempted=True,
        )
