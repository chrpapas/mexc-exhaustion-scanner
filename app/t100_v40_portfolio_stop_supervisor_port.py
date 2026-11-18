"""Production V40 four-slot STOP port for V40PortfolioSupervisor.

Composes frozen completed Min30 exit projection, fresh signed all-position
trailing preflight, durable stop journal, and the separately scoped MEXC
four-slot native-stop POST adapter. It DOES NOT own a Render startup/permit;
the global release remains NOT_CERTIFIED.

Each call may attempt exactly ONE distinct stop tightening. A second due
position is observed on a NEW account/bar cycle. If multiple simultaneous
proposals exist and no independently certified historical order-priority
witness was provided, fail closed; never choose an arbitrary SQL row.

Never declare a successful POST to be durable stop protection. A subsequent
independent StopCompletionHandoff must verify the exact same native stop ID,
all remaining protected slots, immutable checkpoint and cursor revisions.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Protocol

from app import t100_live_guards
from app.t100_live_authorization import (
    LiveMutationPermit,V40_PORTFOLIO_SCOPE,
    require_certified_transport_permit,
)
from app.t100_live_durable_recovery import PositionCheckpoint
from app.t100_live_four_slot_ledger_reader import PostgresFourSlotLedgerReader
from app.t100_live_stop_amendment_coordinator import StopAmendmentLifecycleCoordinator
from app.t100_live_stop_mutation_executor import StopMutationExecutor
from app.t100_live_trailing_preflight import TrailingProposalReview
from app.t100_v40_unreleased_native_stop_cycle import V40UnreleasedStopCycleResult
from app.t100_v40_unreleased_portfolio_exit_batch import V40SlotExitReview
from app.t100_v40_unreleased_verified_exit_handoff import V40VerifiedExitProjection
from app.t100_v40_portfolio_native_stop_transport import (
    V40FourSlotNativeStopAmendTransport,
)


class V40FourSlotStopPortHalted(RuntimeError):
    """A redacted, no-trade code; caller must reconcile UNKNOWN."""


@dataclass(frozen=True)
class V40LiveStopReadback:
    """Current independent source/cursor/ledger-derived preflight snapshot."""

    checkpoints: tuple[PositionCheckpoint,...]
    target_signal_id: int
    preflight: TrailingProposalReview


class V40CurrentStopPreflight(Protocol):
    async def observe(self, position_id:str) -> V40LiveStopReadback: ...


class V40FourSlotStopLifecyclePort:
    """One action, one durable journal, one MEXC POST max, no retry."""

    def __init__(
        self, *, client:Any,ledger:PostgresFourSlotLedgerReader,
        guardian_reader:Any,stop_lifecycle:StopAmendmentLifecycleCoordinator,
        preflight:V40CurrentStopPreflight,
    )->None:
        if (
            type(ledger) is not PostgresFourSlotLedgerReader
            or type(stop_lifecycle) is not StopAmendmentLifecycleCoordinator
            or not callable(getattr(client,"_private_get",None))
            or not callable(getattr(client,"_private_post",None))
            or not callable(getattr(guardian_reader,"verify",None))
            or not callable(getattr(preflight,"observe",None))
        ):
            raise V40FourSlotStopPortHalted(
                "V40_FOUR_SLOT_STOP_PORT_BOUNDARY_INVALID"
            )
        self.client=client
        self.ledger=ledger
        self.guardian=guardian_reader
        self.lifecycle=stop_lifecycle
        self.preflight=preflight

    async def tighten_one_stop(
        self, projection:V40VerifiedExitProjection, *,
        permit:LiveMutationPermit,
    )->V40UnreleasedStopCycleResult:
        # This returns before the provider/SQL/venue on uncertified builds.
        if t100_live_guards.LIVE_NATIVE_STOP_AMENDMENT_STATUS!="CERTIFIED":
            raise V40FourSlotStopPortHalted(
                "V40_FOUR_SLOT_STOP_DOCUMENTED_CONTRACT_UNCERTIFIED"
            )
        try:
            checked=require_certified_transport_permit(permit)
        except Exception:
            raise V40FourSlotStopPortHalted(
                "V40_FOUR_SLOT_STOP_PRODUCTION_PERMIT_UNCERTIFIED"
            ) from None
        if checked.scope!=V40_PORTFOLIO_SCOPE:
            raise V40FourSlotStopPortHalted(
                "V40_FOUR_SLOT_STOP_CANARY_SCOPE_FORBIDDEN"
            )
        if (
            type(projection) is not V40VerifiedExitProjection
            or projection.status!=
                "V40_VERIFIED_OWNED_ONE_TO_FOUR_EXITS_PROJECTED_NONAUTHORIZING"
            or projection.verified_exchange_and_ledger_bookends is not True
            or projection.verified_durable_bar_cursors is not True
            or projection.live_execution_certified is not False
            or projection.stop_amendment_permitted is not False
            or projection.exchange_posts!=0 or projection.database_writes!=0
            or projection.batch.native_close_reconciliation_count!=0
            or projection.batch.proposed_stop_amendment_count!=1
            or projection.candidate_native_stop_amendment_count!=1
            or not 1<=projection.protected_position_count<=4
        ):
            raise V40FourSlotStopPortHalted(
                "V40_FOUR_SLOT_STOP_PROJECTION_NOT_SINGLE_SAFE_ACTION"
            )
        targets=[
            x for x in projection.batch.slots
            if type(x) is V40SlotExitReview
            and x.requires_native_stop_amendment is True
        ]
        if len(targets)!=1:
            raise V40FourSlotStopPortHalted(
                "V40_FOUR_SLOT_STOP_TARGET_NONUNIQUE"
            )
        target=targets[0]
        if (
            target.requires_native_order_reconciliation is not False
            or target.decision.status!=
                "NATIVE_STOP_TIGHTENING_PROPOSED_UNAUTHORIZED"
            or target.proposed_stop_price is None
            or target.proposed_stop_price!=
                target.decision.proposed_native_stop_price
            or target.decision.requires_exchange_stop_amendment is not True
            or target.decision.proposed_checkpoint is None
            or target.decision.proposed_checkpoint.position_id!=target.position_id
            or target.decision.proposed_checkpoint.symbol!=target.symbol
            or target.decision.proposed_checkpoint.native_stop_price!=
                target.proposed_stop_price
            or checked.symbol!=target.symbol
        ):
            raise V40FourSlotStopPortHalted(
                "V40_FOUR_SLOT_STOP_FROZEN_DECISION_MISMATCH"
            )
        try:
            state=await self.preflight.observe(target.position_id)
        except Exception:
            raise V40FourSlotStopPortHalted(
                "V40_FOUR_SLOT_STOP_CURRENT_PREFLIGHT_FAILED"
            ) from None
        if (
            type(state) is not V40LiveStopReadback
            or type(state.checkpoints) is not tuple
            or len(state.checkpoints)!=projection.protected_position_count
            or any(type(x) is not PositionCheckpoint for x in state.checkpoints)
            or type(state.target_signal_id) is not int
            or state.target_signal_id!=checked.signal_id
            or type(state.preflight) is not TrailingProposalReview
        ):
            raise V40FourSlotStopPortHalted(
                "V40_FOUR_SLOT_STOP_CURRENT_OWNER_OR_SIGNAL_INVALID"
            )
        matched=[cp for cp in state.checkpoints
                 if cp.position_id==target.position_id
                 and cp.symbol==target.symbol]
        proposal=state.preflight.proposal
        if (
            len(matched)!=1 or proposal is None
            or proposal.position_id!=target.position_id
            or proposal.symbol!=target.symbol
            or proposal.stop_order_id!=matched[0].stop_order_id
            or proposal.old_stop!=matched[0].stop_price
            or proposal.proposed_stop!=target.proposed_stop_price
            or proposal.best_profit_pct!=
                target.decision.proposed_checkpoint.best_profit_pct
            or proposal.armed is not
                target.decision.proposed_checkpoint.trail_armed
        ):
            raise V40FourSlotStopPortHalted(
                "V40_FOUR_SLOT_STOP_CURRENT_PREFLIGHT_DOES_NOT_MATCH_BAR"
            )

        # The actual exchange transport rechecks current signed inventory,
        # every non-target native stop, exact leader/ledger and guardian.
        transport=V40FourSlotNativeStopAmendTransport(
            client=self.client,checkpoints=state.checkpoints,
            target_position_id=target.position_id,
            target_signal_id=state.target_signal_id,
            preflight=state.preflight,guardian_reader=self.guardian,
            ledger=self.ledger,
        )
        try:
            amendment,journal_state=await self.lifecycle.prepare(proposal)
        except Exception:
            raise V40FourSlotStopPortHalted(
                "V40_FOUR_SLOT_STOP_JOURNAL_PREPARE_FAILED"
            ) from None
        if journal_state!="PREPARED":
            return V40UnreleasedStopCycleResult(
                "V40_TRAIL_EXISTING_ATTEMPT_RECONCILE_ONLY",
                amendment=amendment,requires_independent_reconciliation=True,
            )
        try:
            attempted=await StopMutationExecutor(
                lifecycle=self.lifecycle,transport=transport,
            ).submit_once(amendment,permit=checked)
        except BaseException:
            # PREPARED / SUBMITTING / UNKNOWN needs independent venue GET.
            # No automatic repeat of the same stop POST.
            return V40UnreleasedStopCycleResult(
                "V40_TRAIL_POST_UNKNOWN_RECONCILE_ONLY",
                amendment=amendment,
                stop_post_may_have_occurred=True,
                requires_independent_reconciliation=True,
            )
        if (
            attempted.external_id!=amendment.external_id
            or attempted.journal_state!="SUBMITTING"
            or attempted.post_attempted is not True
        ):
            return V40UnreleasedStopCycleResult(
                "V40_TRAIL_POST_STATUS_UNVERIFIED_RECONCILE_ONLY",
                amendment=amendment,
                stop_post_may_have_occurred=True,
                requires_independent_reconciliation=True,
            )
        return V40UnreleasedStopCycleResult(
            "V40_TRAIL_ONE_STOP_POST_ATTEMPT_RECONCILE_ONLY",
            amendment=amendment,stop_post_may_have_occurred=True,
            requires_independent_reconciliation=True,
        )
