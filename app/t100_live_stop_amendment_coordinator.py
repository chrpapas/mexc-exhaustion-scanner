"""Crash-safe stop-amendment lifecycle coordination without exchange writes.

This module deliberately has NO MEXC POST/cancel/change method. It prepares
and advances durable stop-amendment journal state, then reconciles already-
observed exchange state against the durable intent.

Required future mutation sequence:
  PREPARED -> SUBMITTING -> external mutation attempt -> VERIFIED or UNKNOWN.

A transition observed while still PREPARED is not accepted as trader-owned;
that would be indistinguishable from manual/external drift.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from app.t100_live_expected_stop_transition import (
    ExpectedStopTransition,
    assess_expected_stop_current_outcome,
    assess_expected_stop_transition,
)
from app.t100_live_stop_journal import (
    StopAmendment,
    StopJournal,
    StopJournalHalted,
)
from app.t100_live_supervisor import StopProposal


class StopAmendmentCoordinatorHalted(RuntimeError):
    pass


@dataclass(frozen=True)
class StopReconciliationOutcome:
    external_id: str
    verified_stop_order_id: str
    status: str
    replacement_stop_identity: bool
    live_authorization: bool = False


class StopAmendmentLifecycleCoordinator:
    def __init__(self, journal: StopJournal):
        self.journal = journal

    async def prepare(
        self,
        proposal: StopProposal,
    ) -> tuple[StopAmendment, str]:
        if not isinstance(proposal, StopProposal):
            raise StopAmendmentCoordinatorHalted(
                "STOP_PROPOSAL_INVALID"
            )
        amendment = StopAmendment.build(
            symbol=proposal.symbol,
            position_id=proposal.position_id,
            stop_order_id=proposal.stop_order_id,
            previous_stop=proposal.old_stop,
            proposed_stop=proposal.proposed_stop,
        )
        try:
            inserted = await self.journal.prepare(amendment)
            if inserted:
                return amendment, "PREPARED"
            state = await self.journal.state(amendment.external_id)
        except Exception as exc:
            raise StopAmendmentCoordinatorHalted(
                "STOP_JOURNAL_UNAVAILABLE"
            ) from exc
        if state not in {"PREPARED", "SUBMITTING", "UNKNOWN", "VERIFIED"}:
            raise StopAmendmentCoordinatorHalted(
                "STOP_JOURNAL_STATE_INVALID"
            )
        return amendment, f"EXISTING_{state}"

    async def mark_submitting(self, amendment: StopAmendment) -> str:
        state = await self._state(amendment)
        if state == "SUBMITTING":
            return "SUBMITTING"
        if state != "PREPARED":
            raise StopAmendmentCoordinatorHalted(
                "STOP_SUBMISSION_REQUIRES_PREPARED"
            )
        try:
            changed = await self.journal.transition(
                amendment.external_id,
                "PREPARED",
                "SUBMITTING",
            )
        except Exception as exc:
            raise StopAmendmentCoordinatorHalted(
                "STOP_JOURNAL_UNAVAILABLE"
            ) from exc
        if not changed:
            raise StopAmendmentCoordinatorHalted(
                "STOP_SUBMITTING_CAS_FAILURE"
            )
        return "SUBMITTING"

    async def mark_unknown(self, amendment: StopAmendment) -> str:
        state = await self._state(amendment)
        if state == "UNKNOWN":
            return "UNKNOWN"
        if state != "SUBMITTING":
            raise StopAmendmentCoordinatorHalted(
                "STOP_UNKNOWN_REQUIRES_SUBMITTING"
            )
        try:
            changed = await self.journal.transition(
                amendment.external_id,
                "SUBMITTING",
                "UNKNOWN",
            )
        except Exception as exc:
            raise StopAmendmentCoordinatorHalted(
                "STOP_JOURNAL_UNAVAILABLE"
            ) from exc
        if not changed:
            raise StopAmendmentCoordinatorHalted(
                "STOP_UNKNOWN_CAS_FAILURE"
            )
        return "UNKNOWN"

    async def reconcile_observed_transition(
        self,
        amendment: StopAmendment,
        *,
        before_stops: Any,
        after_stops: Any,
        finished_stops: Any | None = None,
    ) -> StopReconciliationOutcome:
        state = await self._state(amendment)
        if state == "PREPARED":
            raise StopAmendmentCoordinatorHalted(
                "STOP_CHANGE_NOT_OWNED_JOURNAL_NOT_SUBMITTING"
            )
        if state not in {"SUBMITTING", "UNKNOWN", "VERIFIED"}:
            raise StopAmendmentCoordinatorHalted(
                "STOP_JOURNAL_STATE_INVALID"
            )

        expected = ExpectedStopTransition.build(
            external_id=amendment.external_id,
            symbol=amendment.symbol,
            position_id=amendment.position_id,
            previous_stop_order_id=amendment.stop_order_id,
            previous_stop=amendment.previous_stop,
            proposed_stop=amendment.proposed_stop,
        )
        report = assess_expected_stop_transition(
            before_stops=before_stops,
            after_stops=after_stops,
            expected=expected,
            finished_stops=finished_stops,
        )
        if report.get("expected_transition_verified") is not True:
            raise StopAmendmentCoordinatorHalted(
                f"STOP_TRANSITION_UNVERIFIED:{report.get('status', 'UNKNOWN')}"
            )

        return await self._finalize_verified_report(
            amendment,
            state=state,
            current_stops=after_stops,
            report=report,
        )

    async def reconcile_current_outcome(
        self,
        amendment: StopAmendment,
        *,
        current_stops: Any,
        finished_stops: Any | None = None,
    ) -> StopReconciliationOutcome:
        """Reconcile after restart without a pre-mutation REST snapshot.

        Only SUBMITTING/UNKNOWN/VERIFIED journal states are eligible. The
        durable amendment itself is the prior-state witness. An unchanged
        current stop remains unresolved and is never permission to retry.
        """
        state = await self._state(amendment)
        if state == "PREPARED":
            raise StopAmendmentCoordinatorHalted(
                "STOP_CHANGE_NOT_OWNED_JOURNAL_NOT_SUBMITTING"
            )
        if state not in {"SUBMITTING", "UNKNOWN", "VERIFIED"}:
            raise StopAmendmentCoordinatorHalted(
                "STOP_JOURNAL_STATE_INVALID"
            )

        expected = ExpectedStopTransition.build(
            external_id=amendment.external_id,
            symbol=amendment.symbol,
            position_id=amendment.position_id,
            previous_stop_order_id=amendment.stop_order_id,
            previous_stop=amendment.previous_stop,
            proposed_stop=amendment.proposed_stop,
        )
        report = assess_expected_stop_current_outcome(
            current_stops=current_stops,
            expected=expected,
            finished_stops=finished_stops,
        )
        if report.get("expected_transition_verified") is not True:
            raise StopAmendmentCoordinatorHalted(
                f"STOP_CURRENT_OUTCOME_UNVERIFIED:{report.get('status', 'UNKNOWN')}"
            )
        return await self._finalize_verified_report(
            amendment,
            state=state,
            current_stops=current_stops,
            report=report,
        )

    async def _finalize_verified_report(
        self,
        amendment: StopAmendment,
        *,
        state: str,
        current_stops: Any,
        report: dict[str, Any],
    ) -> StopReconciliationOutcome:
        candidates = [
            row
            for row in current_stops
            if isinstance(row, dict)
            and str(row.get("positionId") or "") == amendment.position_id
            and row.get("symbol") == amendment.symbol
        ]
        if len(candidates) != 1:
            raise StopAmendmentCoordinatorHalted(
                "VERIFIED_STOP_RESULT_NOT_UNIQUE"
            )
        resulting_id = str(
            candidates[0].get("id")
            or candidates[0].get("stopPlanOrderId")
            or ""
        )
        if not resulting_id:
            raise StopAmendmentCoordinatorHalted(
                "VERIFIED_STOP_RESULT_ID_MISSING"
            )

        if state == "VERIFIED":
            try:
                persisted_id = await self.journal.verified_stop_order_id(
                    amendment.external_id
                )
            except Exception as exc:
                raise StopAmendmentCoordinatorHalted(
                    "STOP_JOURNAL_UNAVAILABLE"
                ) from exc
            if persisted_id != resulting_id:
                raise StopAmendmentCoordinatorHalted(
                    "VERIFIED_STOP_ID_PERSISTENCE_MISMATCH"
                )
        else:
            try:
                changed = await self.journal.mark_verified(
                    amendment.external_id,
                    state,
                    resulting_id,
                )
            except StopJournalHalted as exc:
                raise StopAmendmentCoordinatorHalted(
                    f"STOP_VERIFICATION_PERSISTENCE_BLOCKED:{exc}"
                ) from exc
            except Exception as exc:
                raise StopAmendmentCoordinatorHalted(
                    "STOP_JOURNAL_UNAVAILABLE"
                ) from exc
            if not changed:
                raise StopAmendmentCoordinatorHalted(
                    "STOP_VERIFIED_CAS_FAILURE"
                )

        return StopReconciliationOutcome(
            external_id=amendment.external_id,
            verified_stop_order_id=resulting_id,
            status=str(report["status"]),
            replacement_stop_identity=bool(
                report.get("replacement_stop_identity")
            ),
        )

    async def _state(self, amendment: StopAmendment) -> str:
        if not isinstance(amendment, StopAmendment):
            raise StopAmendmentCoordinatorHalted(
                "STOP_AMENDMENT_INVALID"
            )
        try:
            return await self.journal.state(amendment.external_id)
        except Exception as exc:
            raise StopAmendmentCoordinatorHalted(
                "STOP_JOURNAL_UNAVAILABLE"
            ) from exc
