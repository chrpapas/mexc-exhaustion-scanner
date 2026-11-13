"""UNRELEASED V40 trail amendment: typed bridge to existing durable stop tools.

This module is NOT imported by the Render trader and is not a CLI/worker.
The live native-stop amendment contract remains UNVERIFIED_CURRENT_DOCS,
so this bridge returns a hard halt BEFORE any DB writes or exchange POST.

Once independently certified, an external reviewed supervisor must provide
a signed fill/restart trace, consecutive Min30 decisions, stable full-account
signed GETs and a typed signal-bound mutation permit. The existing durable
journal ensures a POST is attempted once, and the existing GET-only handoff
must certify and persist a change before any further exit decisions.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from app import t100_live_guards
from app.t100_live_authorization import (
    LiveMutationPermit, require_certified_transport_permit, LiveAuthorizationHalted,
    V40_CANARY_SCOPE,
)
from app.t100_live_durable_recovery import PositionCheckpoint
from app.t100_live_stop_amendment_coordinator import StopAmendmentLifecycleCoordinator
from app.t100_live_stop_journal import StopAmendment
from app.t100_live_stop_mutation_executor import StopMutationExecutor
from app.t100_live_trailing_preflight import TrailingProposalReview
from app.t100_live_v40_trailing_restart import V40TrailingRestartView
from app.t100_live_v40_stop_completion_handoff import V40NativeStopCompletionHandoff
from app.t100_v40_unreleased_exit_policy import V40ExitDecision


class V40UnreleasedStopCycleHalted(RuntimeError):
    pass


@dataclass(frozen=True)
class V40UnreleasedStopCycleResult:
    status: str
    amendment: StopAmendment | None = None
    stop_post_may_have_occurred: bool = False
    requires_independent_reconciliation: bool = False
    orders_sent: int = 0
    new_entries_allowed: bool = False
    live_authorization: bool = False


def _require_contract_gate(permit: LiveMutationPermit, *, symbol: str, signal_id: int) -> None:
    # Immutable production default. Never flip this on the first trade's
    # production branch merely to make an offline test succeed.
    if t100_live_guards.LIVE_NATIVE_STOP_AMENDMENT_STATUS != "CERTIFIED":
        raise V40UnreleasedStopCycleHalted(
            "V40_TRAIL_NATIVE_AMENDMENT_EXCHANGE_CONTRACT_NOT_CERTIFIED"
        )
    try:
        permit = require_certified_transport_permit(permit)
    except LiveAuthorizationHalted:
        raise V40UnreleasedStopCycleHalted("V40_TRAIL_TYPED_PERMIT_INVALID") from None
    if (
        permit.scope != V40_CANARY_SCOPE
        or permit.symbol != symbol
        or permit.signal_id != signal_id
    ):
        raise V40UnreleasedStopCycleHalted("V40_TRAIL_SIGNAL_SCOPE_MISMATCH")


async def submit_one_verified_trailing_amendment(
    *,
    signal_id: int,
    previous: PositionCheckpoint,
    planned: V40ExitDecision,
    preflight: TrailingProposalReview,
    restart: V40TrailingRestartView,
    permit: LiveMutationPermit,
    lifecycle: StopAmendmentLifecycleCoordinator,
    mutation: StopMutationExecutor,
) -> V40UnreleasedStopCycleResult:
    """At most one POST after durable PREPARED -> SUBMITTING; NEVER retry.

    This is a bridge, NOT the upstream stream/GET verifier. A live caller must
    independently capture and check all evidence BEFORE passing arguments.
    """
    if (
        type(signal_id) is not int or signal_id < 1
        or type(previous) is not PositionCheckpoint
        or type(planned) is not V40ExitDecision
        or type(preflight) is not TrailingProposalReview
        or type(restart) is not V40TrailingRestartView
        or type(lifecycle) is not StopAmendmentLifecycleCoordinator
        or type(mutation) is not StopMutationExecutor
    ):
        raise V40UnreleasedStopCycleHalted("V40_TRAIL_STAGED_INPUT_INVALID")
    _require_contract_gate(permit, symbol=previous.symbol, signal_id=signal_id)

    next_state = planned.proposed_checkpoint
    observed = preflight.proposal
    if not (
        planned.status == "NATIVE_STOP_TIGHTENING_PROPOSED_UNAUTHORIZED"
        and planned.requires_exchange_stop_amendment is True
        and planned.live_authorization is False
        and next_state is not None
        and observed is not None
        and preflight.status == "TRAILING_PROPOSAL_OBSERVED_NONAUTHORIZING"
        and preflight.account_bookends_equal is True
        and preflight.all_owned_full_volume_market_stops_observed is True
        and preflight.frozen_trailing_policy_matched is True
        and preflight.current_stop_above_last_price is True
        and restart.status == "V40_TRAILING_RESTART_CANDIDATE_REBUILT_NONAUTHORIZING"
        and restart.durable_revision_history_verified is True
        and restart.post_fill_low_stream_complete is True
        and restart.stop_amendment_authorized is False
        and previous.lifecycle_state == "OPEN"
        and previous.checkpoint_version == restart.checkpoint_version
        and previous.position_id == observed.position_id == restart.position_id == next_state.position_id
        and previous.symbol == observed.symbol == restart.symbol == next_state.symbol
        and previous.stop_order_id == observed.stop_order_id
        and previous.stop_price == observed.old_stop == restart.recovered_stop_price
        and previous.best_profit_pct == restart.recovered_best_profit_pct
        and previous.armed == restart.recovered_armed
        and next_state.best_profit_pct == observed.best_profit_pct == restart.next_best_profit_pct
        and next_state.trail_armed == observed.armed == restart.next_armed
        and next_state.native_stop_price == observed.proposed_stop
        and next_state.native_stop_price == planned.proposed_native_stop_price
        and next_state.native_stop_price == restart.proposed_stop_price
        and next_state.native_stop_price < previous.stop_price
    ):
        raise V40UnreleasedStopCycleHalted("V40_TRAIL_CANDIDATE_OR_VENUE_PROVENANCE_NOT_MATCHED")

    # Preflight must have certified two complete, stable native-STOP reads;
    # the shared Postgres journal provides same-account CAS for POST.
    try:
        amendment, state = await lifecycle.prepare(observed)
    except Exception:
        raise V40UnreleasedStopCycleHalted("V40_TRAIL_PREPARE_FAILED_NO_POST") from None
    if state != "PREPARED":
        # PREPARED-from-an-earlier-call is not safe to claim as ours.
        return V40UnreleasedStopCycleResult(
            "V40_TRAIL_EXISTING_ATTEMPT_RECONCILE_ONLY",
            amendment=amendment,requires_independent_reconciliation=True,
        )
    try:
        attempt = await mutation.submit_once(amendment, permit=permit)
    except Exception:
        # SUBMITTING may have reached exchange. Reconciliation only.
        return V40UnreleasedStopCycleResult(
            "V40_TRAIL_POST_UNKNOWN_RECONCILE_ONLY",
            amendment=amendment,stop_post_may_have_occurred=True,
            requires_independent_reconciliation=True,
        )
    if (
        attempt.external_id != amendment.external_id
        or attempt.journal_state != "SUBMITTING"
        or attempt.post_attempted is not True
    ):
        return V40UnreleasedStopCycleResult(
            "V40_TRAIL_POST_STATUS_UNVERIFIED_RECONCILE_ONLY",
            amendment=amendment,stop_post_may_have_occurred=True,
            requires_independent_reconciliation=True,
        )
    return V40UnreleasedStopCycleResult(
        "V40_TRAIL_ONE_STOP_POST_ATTEMPT_RECONCILE_ONLY",
        amendment=amendment,stop_post_may_have_occurred=True,
        requires_independent_reconciliation=True,
    )


async def reconcile_native_stop_after_one_attempt(
    *,
    amendment: StopAmendment,
    previous: PositionCheckpoint,
    restart: V40TrailingRestartView,
    permit: LiveMutationPermit,
    handoff: V40NativeStopCompletionHandoff,
) -> V40UnreleasedStopCycleResult:
    """GET-only stop outcome verification and durable checkpoint commit.

    The handoff already enforces strict current native stop identity,
    full-volume coverage, account completeness, immutable revision
    history, no changed position origin and signed GET bookends.
    """
    if (
        type(previous) is not PositionCheckpoint
        or type(amendment) is not StopAmendment
        or type(restart) is not V40TrailingRestartView
        or type(handoff) is not V40NativeStopCompletionHandoff
    ):
        raise V40UnreleasedStopCycleHalted("V40_TRAIL_POSTFLIGHT_INPUT_INVALID")
    _require_contract_gate(permit, symbol=previous.symbol, signal_id=permit.signal_id)
    try:
        outcome = await handoff.commit_in_place_if_verified(
            amendment=amendment, previous=previous,
            trailing=restart, permit=permit,
        )
    except Exception:
        raise V40UnreleasedStopCycleHalted(
            "V40_TRAIL_POSTFLIGHT_UNVERIFIED_RECONCILE_ONLY"
        ) from None
    if outcome.journal_checkpoint_committed is not True:
        return V40UnreleasedStopCycleResult(
            "V40_TRAIL_POSTFLIGHT_NOT_COMMITTED_RECONCILE_ONLY",
            amendment=amendment,requires_independent_reconciliation=True,
        )
    return V40UnreleasedStopCycleResult(
        "V40_TRAIL_STOP_AND_DURABLE_CHECKPOINT_GET_VERIFIED",
        amendment=amendment,requires_independent_reconciliation=False,
    )
