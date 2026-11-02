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
from decimal import Decimal
from typing import Any

from app.t100_live_active_tpsl import effective_protective_quantity

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

    @staticmethod
    def _require_native_short_stop_coverage(
        amendment: StopAmendment,
        stops: Any,
        *,
        position_quantity: Decimal,
    ) -> None:
        """An exchange price match without complete native SL is not VERIFIED.

        The journal must never claim a native-stop mutation succeeded unless
        the resulting active short-side stop protects the FULL owned position.
        This guard also applies to direct coordinator callers that bypass
        the higher-level checkpoint persistence pipeline.
        """
        if (
            type(position_quantity) is not Decimal
            or not position_quantity.is_finite()
            or position_quantity <= 0
            or not isinstance(stops, list)
            or any(type(row) is not dict for row in stops)
        ):
            raise StopAmendmentCoordinatorHalted(
                "STOP_RECONCILIATION_COVERAGE_EVIDENCE_INVALID"
            )
        matching = [
            row for row in stops
            if str(row.get("positionId") or "") == amendment.position_id
            and row.get("symbol") == amendment.symbol
        ]
        if not matching:
            raise StopAmendmentCoordinatorHalted(
                "PROTECTION_GAP_NOT_ACCEPTED"
            )
        if len(matching) != 1:
            raise StopAmendmentCoordinatorHalted(
                "STOP_RECONCILIATION_STOP_NOT_UNIQUE"
            )
        stop = matching[0]
        if (
            type(stop.get("state")) is not int or stop["state"] != 1
            or type(stop.get("isFinished")) is not int
            or stop["isFinished"] != 0
            or type(stop.get("positionType")) is not int
            or stop["positionType"] != 2
            or effective_protective_quantity(
                stop, position_quantity=position_quantity,
            ) != position_quantity
        ):
            raise StopAmendmentCoordinatorHalted(
                "STOP_RECONCILIATION_NATIVE_FULL_COVERAGE_UNVERIFIED"
            )

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

    async def state(self, amendment: StopAmendment) -> str:
        """Return durable amendment state through the coordinator boundary."""
        return await self._state(amendment)

    async def mark_submitting(self, amendment: StopAmendment) -> str:
        state = await self._state(amendment)
        # Never regard an already-SUBMITTING journal as our CAS victory.
        # Another worker may have changed PREPARED -> SUBMITTING between the
        # caller's first read and this one. Returning success would allow a
        # second real stop mutation POST for the same durable amendment.
        if state == "SUBMITTING":
            raise StopAmendmentCoordinatorHalted(
                "STOP_SUBMISSION_ALREADY_STARTED_RECONCILE_ONLY"
            )
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
        position_quantity: Decimal,
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
        self._require_native_short_stop_coverage(
            amendment, before_stops, position_quantity=position_quantity,
        )
        self._require_native_short_stop_coverage(
            amendment, after_stops, position_quantity=position_quantity,
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
        position_quantity: Decimal,
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
        self._require_native_short_stop_coverage(
            amendment, current_stops, position_quantity=position_quantity,
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
