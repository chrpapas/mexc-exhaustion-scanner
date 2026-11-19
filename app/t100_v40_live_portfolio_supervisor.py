"""V40 four-slot live orchestration, release-gated and one irreversible action/cycle.

Uses existing fenced V40OneShotCoordinator for entries, durable native-stop
amendment journal for trailing, immutable cursor checkpoint persistence for
unchanged bars, and native-close reconciliation for suspected exits.

No credentials, permits, exchange clients or SQL connections are created here.
Every cycle must come from NEW signed account GET/ledger observations. Any
ambiguous result HALTS; neither a possibly filled stop nor a protected entry
allows another order without re-observation. This supervisor is intentionally
unregistered in Render until real four-slot permit and risk witnesses exist.
"""
from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Any, Awaitable, Callable, Protocol

from app.t100_live_authorization import LiveMutationPermit, require_certified_transport_permit
from app.t100_live_execution import LiveCaps
from app.t100_live_four_slot_snapshot_coordinator import FourSlotSnapshotResult
from app.t100_live_v40_entry_coordinator import V40OneShotReport
from app.t100_live_v40_stage2_source import V40SourceBatch, V40SourceCandidate
from app.t100_v40_guardian_post_gate import V40GuardianPostGateEvidence
from app.t100_v40_unreleased_nonflat_guardian_evidence import V40NonflatGuardianEvidence
from app.t100_v40_unreleased_verified_exit_handoff import V40VerifiedExitProjection
from app.t100_v40_unreleased_native_stop_cycle import V40UnreleasedStopCycleResult
from app.t100_v40_unreleased_portfolio_native_close import V40PortfolioCloseWitness
from app.t100_v40_native_close_mismatch_recovery import V40NativeCloseMismatchEvidence
from app.t100_live_durable_recovery import PositionCheckpoint
from app.t100_live_leader_fencing import LeaderLease
from app.t100_v40_forward_close_finalizer import V40ForwardCloseResult
from app.t100_v40_native_close_release_handoff import (
    V40NativeCloseReleaseHandoffReport,
)

# This is not an existing permit scope. New four-slot issuance and the
# corresponding final MEXC transport gate must be independently reviewed.
# The single-canary permit MUST NOT be promoted to autonomous portfolio use.
PORTFOLIO_SCOPE = "T100_V40_FOUR_SLOT_PRODUCTION_TRIAL"
FLAT_GOOD = "V40_GUARDIAN_POST_GATE_CURRENT_FLAT_UNLATCHED_NONAUTHORIZING"
OWNED_GOOD = "V40_NONFLAT_GUARDIAN_PROTECTED_OBSERVER_FRESH_NONAUTHORIZING"
SOURCE_GOOD = "FROZEN_V40_STAGE2_SOURCE_VALIDATED_NONAUTHORIZING"


@dataclass(frozen=True)
class V40PortfolioCycleFrame:
    snapshot: FourSlotSnapshotResult
    guardian: V40GuardianPostGateEvidence | V40NonflatGuardianEvidence
    source: V40SourceBatch
    completed_exit: V40VerifiedExitProjection | None
    # Must be computed from each current, immutable completed-bar cursor.
    # This is a source contract, not a config flag or permission.
    no_bar_due_ids: tuple[str, ...] = ()


@dataclass(frozen=True)
class V40CursorAdvanceResult:
    status: str
    position_ids: tuple[str, ...] = ()
    durable_commit_verified: bool = False
    stop_bookends_verified: bool = False
    exchange_posts: int = 0


@dataclass(frozen=True)
class V40PortfolioSupervisorReport:
    status: str
    observed_slots: int = 0
    signal_id: int | None = None
    origin_may_have_mutated: bool = False
    stop_post_may_have_occurred: bool = False
    must_reconcile_before_next_cycle: bool = False
    entries_allowed: bool = False
    live_authorization: bool = False


class V40PortfolioPorts(Protocol):
    async def observe(
        self,
    ) -> V40PortfolioCycleFrame | V40NativeCloseMismatchEvidence: ...
    async def persist_unchanged_stop_checkpoint(
        self, projection: V40VerifiedExitProjection, *,
        permit: LiveMutationPermit,
    ) -> V40CursorAdvanceResult: ...
    async def reconcile_native_close(
        self, projection: V40VerifiedExitProjection,
    ) -> V40PortfolioCloseWitness: ...
    async def release_verified_native_close(
        self, projection: V40VerifiedExitProjection,
        witness: V40PortfolioCloseWitness, *,
        permit: LiveMutationPermit,
    ) -> V40NativeCloseReleaseHandoffReport: ...
    async def tighten_one_stop(
        self, projection: V40VerifiedExitProjection, *, permit: LiveMutationPermit,
    ) -> V40UnreleasedStopCycleResult: ...
    async def enter_one(
        self, *, permit: LiveMutationPermit, caps: LiveCaps,
        parity_report: dict[str, Any], holder_id: str,
        monitored_signal_id: int, monitored_symbol: str,
        monitored_tier: str,
    ) -> V40OneShotReport: ...


class V40PortfolioSupervisor:
    """Serialized state machine; no same-cycle stop+entry or exit+entry."""

    def __init__(
        self, ports: V40PortfolioPorts, *,
        caps: LiveCaps, parity_report: dict[str, Any], holder_id: str,
    ) -> None:
        if (
            not callable(getattr(ports, "observe", None))
            or type(caps) is not LiveCaps
            or type(parity_report) is not dict
            or type(holder_id) is not str or not holder_id.strip()
        ):
            raise ValueError("V40_PORTFOLIO_SUPERVISOR_CONFIG_INVALID")
        self.ports, self.caps, self.parity, self.holder = (
            ports, caps, parity_report, holder_id,
        )

    @staticmethod
    def _halt(
        code: str, *, slots: int = 0, signal: int | None = None,
        entry: bool = False, stop: bool = False, reconcile: bool = False,
    ) -> V40PortfolioSupervisorReport:
        return V40PortfolioSupervisorReport(
            code, observed_slots=slots, signal_id=signal,
            origin_may_have_mutated=entry,
            stop_post_may_have_occurred=stop,
            must_reconcile_before_next_cycle=entry or stop or reconcile,
        )

    @staticmethod
    def _source_valid(source: object) -> bool:
        return (
            type(source) is V40SourceBatch
            and source.status == SOURCE_GOOD
            and source.history_complete is True
            and source.read_only_repeatable_read is True
            and source.entry_allowed is False
            and source.live_authorization is False
            and type(source.candidates) is tuple
            and all(type(x) is V40SourceCandidate for x in source.candidates)
            and len({x.signal_id for x in source.candidates}) == len(source.candidates)
            and len({x.symbol for x in source.candidates}) == len(source.candidates)
            and all(
                type(x.signal_id) is int and x.signal_id > 0
                and type(x.observed_age_seconds) in (int, float)
                and 0 <= x.observed_age_seconds <= 900
                and x.tier in ("LOW_100", "HIGH_225")
                and x.entry_allowed is False and x.live_authorization is False
                for x in source.candidates
            )
        )

    @classmethod
    def _frame_valid(cls, frame: object) -> bool:
        if type(frame) is not V40PortfolioCycleFrame:
            return False
        snap = frame.snapshot
        if (
            type(snap) is not FourSlotSnapshotResult
            or snap.entry_allowed is not False or snap.live_authorization is not False
            or type(snap.occupied_slots) is not int
            or not 0 <= snap.occupied_slots <= 4
            or not cls._source_valid(frame.source)
            or type(frame.no_bar_due_ids) is not tuple
            or any(type(pid) is not str or not pid for pid in frame.no_bar_due_ids)
            or len(set(frame.no_bar_due_ids)) != len(frame.no_bar_due_ids)
        ):
            return False
        if snap.occupied_slots == 0:
            g = frame.guardian
            return (
                snap.status == "FLAT_OBSERVED_NONAUTHORIZING"
                and snap.recovery is None
                and frame.completed_exit is None and frame.no_bar_due_ids == ()
                and type(g) is V40GuardianPostGateEvidence
                and g.status == FLAT_GOOD and g.state_verified is True
                and g.outbox_verified is True and g.guardian_fresh is True
                and g.critical_latch_absent is True
                and g.entry_allowed is False and g.live_authorization is False
                and g.mexc_posts == g.database_writes == 0
            )
        g = frame.guardian
        if not (
            snap.status == "NONFLAT_PROVENANCE_MATCHED_NONAUTHORIZING"
            and snap.recovery is not None
            and len(snap.recovery.recovered) == snap.occupied_slots
            and type(g) is V40NonflatGuardianEvidence
            and g.status == OWNED_GOOD
            and g.guardian_protected_observation_fresh is True
            and g.no_current_scope_critical_or_pending_incidents is True
            and g.next_entry_allowed is False and g.live_authorization is False
            and g.exchange_posts == g.database_writes == 0
        ):
            return False
        ids = tuple(p.position_id for p in snap.recovery.recovered)
        if len(set(ids)) != len(ids):
            return False
        projection = frame.completed_exit
        if projection is None:
            return set(frame.no_bar_due_ids) == set(ids)
        return (
            type(projection) is V40VerifiedExitProjection
            and projection.status ==
                "V40_VERIFIED_OWNED_ONE_TO_FOUR_EXITS_PROJECTED_NONAUTHORIZING"
            and projection.protected_position_count == len(ids)
            and projection.verified_exchange_and_ledger_bookends is True
            and projection.verified_durable_bar_cursors is True
            and projection.live_execution_certified is False
            and projection.entries_permitted is False
            and projection.stop_amendment_permitted is False
            and projection.position_release_permitted is False
            and projection.exchange_posts == projection.database_writes == 0
            # Each cursor advances independently, but all *other* open
            # positions must remain accounted for and fully protected.
            and 1 <= len(projection.batch.slots) <= len(ids)
            and len({s.position_id for s in projection.batch.slots})
                == len(projection.batch.slots)
            and set(frame.no_bar_due_ids).isdisjoint(
                {s.position_id for s in projection.batch.slots}
            )
            and (
                set(frame.no_bar_due_ids) |
                {s.position_id for s in projection.batch.slots}
            ) == set(ids)
        )

    @staticmethod
    def _permit_valid(permit: LiveMutationPermit | None) -> bool:
        if type(permit) is not LiveMutationPermit or permit.scope != PORTFOLIO_SCOPE:
            return False
        try:
            require_certified_transport_permit(permit)
        except Exception:
            return False
        return True

    async def run_one(
        self, *, permit: LiveMutationPermit | None = None,
        close_lease: LeaderLease | None = None,
    ) -> V40PortfolioSupervisorReport:
        try:
            frame = await self.ports.observe()
        except Exception:
            return self._halt("V40_SUPERVISOR_SIGNED_GET_OR_SQL_HALT")
        if type(frame) is V40NativeCloseMismatchEvidence:
            # A real MEXC native stop can close BETWEEN Min30 bars. At
            # that point the normal strict 0..4-slot recovery deliberately
            # reports one fewer exchange position than SQL OPEN checkpoints.
            # The separate reader proves one exact terminal close and the
            # other native stops, WITHOUT pretending the slot is free.
            # Entry and stop mutation are suspended until actual signed
            # accounting, leader fencing and SQL RELEASE complete.
            witness=frame.witness
            previous=frame.previous
            valid=(
                frame.status==
                "V40_EXACT_ONE_NATIVE_EXIT_POSITIVE_ACCOUNTING_AND_RELEASE_PENDING"
                and frame.ledger_bookends_equal is True
                and frame.venue_bookends_equal is True
                and frame.positive_native_close_verified is True
                and frame.funding_and_final_accounting_verified is False
                and frame.slot_release_permitted is False
                and frame.next_entry_allowed is False
                and frame.live_authorization is False
                and frame.database_writes==0 and frame.mexc_posts==0
                and type(previous) is PositionCheckpoint
                and previous.lifecycle_state=="OPEN"
                and type(frame.survivors) is tuple
                and 0<=len(frame.survivors)<=3
                and previous.position_id not in {
                    p.position_id for p in frame.survivors
                }
                and all(type(p) is PositionCheckpoint
                        and p.lifecycle_state=="OPEN" for p in frame.survivors)
                and type(witness) is V40PortfolioCloseWitness
                and witness.closed_position_id==previous.position_id
                and witness.surviving_protected_slots==len(frame.survivors)
                and witness.exact_closed_position_history_verified is True
                and witness.portfolio_unchanged_across_bookends is True
                and witness.slot_released is False
                and witness.live_authorization is False
                and witness.mexc_posts==witness.database_writes==0
                and witness.closing_fills is not None
                and witness.native_terminal is not None
            )
            if not valid:
                return self._halt(
                    "V40_SUPERVISOR_NATIVE_EXIT_MISMATCH_INVALID",
                    reconcile=True,
                )
            if close_lease is None:
                return self._halt(
                    "V40_SUPERVISOR_NATIVE_EXIT_POSITIVE_SETTLEMENT_REQUIRED",
                    slots=len(frame.survivors)+1,reconcile=True,
                )
            if (
                type(close_lease) is not LeaderLease
                or close_lease.lease_name!="t100-live-executor"
                or type(close_lease.fencing_token) is not int
                or close_lease.fencing_token<=0
                or close_lease.live_authorization is not False
                or not callable(getattr(
                    self.ports,"finalize_verified_forward_native_close",None
                ))
            ):
                return self._halt(
                    "V40_SUPERVISOR_NATIVE_EXIT_FENCED_SETTLEMENT_UNAVAILABLE",
                    slots=len(frame.survivors)+1,reconcile=True,
                )
            try:
                settled=await self.ports.finalize_verified_forward_native_close(
                    lease=close_lease,
                )
            except BaseException:
                # This may follow a DB COMMIT and must never be retried
                # automatically. Only a fresh full inventory and immutable
                # ledger reconciliation can decide the actual outcome.
                return self._halt(
                    "V40_SUPERVISOR_NATIVE_EXIT_DB_OUTCOME_UNKNOWN_RECONCILE",
                    slots=len(frame.survivors)+1,reconcile=True,
                )
            if not (
                type(settled) is V40ForwardCloseResult
                and settled.status==
                   "V40_FORWARD_NATIVE_CLOSE_DURABLY_ACCOUNTED_AND_SLOT_RELEASED_REOBSERVE"
                and settled.position_id==previous.position_id
                and settled.entry_and_close_fills_persisted is True
                and settled.funding_persisted is True
                and settled.final_accounting_persisted is True
                and settled.slot_release_committed is True
                and settled.post_release_new_account_get_required is True
                and settled.entry_allowed is False
                and settled.live_authorization is False
                and settled.mexc_posts==0
                and settled.database_commit_may_have_occurred is True
            ):
                return self._halt(
                    "V40_SUPERVISOR_NATIVE_EXIT_SETTLEMENT_ACK_UNVERIFIED",
                    slots=len(frame.survivors)+1,reconcile=True,
                )
            return self._halt(
                "V40_SUPERVISOR_NATIVE_EXIT_ACCOUNTED_SLOT_RELEASED_REOBSERVE",
                slots=len(frame.survivors)+1,reconcile=True,
            )
        if not self._frame_valid(frame):
            return self._halt("V40_SUPERVISOR_GUARDIAN_ACCOUNT_OR_SOURCE_HALT")
        slots = frame.snapshot.occupied_slots
        projection = frame.completed_exit
        if slots and projection is not None:
            batch = projection.batch
            if batch.native_close_reconciliation_count:
                # Candle touch is NOT an exchange close or slot-release proof.
                try:
                    closed = await self.ports.reconcile_native_close(projection)
                except BaseException:
                    return self._halt("V40_SUPERVISOR_CLOSE_OUTCOME_UNKNOWN", slots=slots)
                if (
                    type(closed) is not V40PortfolioCloseWitness
                    or closed.slot_released is not False
                    or closed.live_authorization is not False
                    or closed.mexc_posts != 0 or closed.database_writes != 0
                ):
                    return self._halt("V40_SUPERVISOR_CLOSE_RESULT_INVALID", slots=slots)
                # Only ONE exact positively evidenced CLOSED short may
                # advance to the existing atomic DB release adapter. A
                # missing/untested/ambiguous native stop or two simultaneous
                # suspected closes cannot authorize a position slot release.
                due=tuple(
                    x for x in batch.slots
                    if x.requires_native_order_reconciliation is True
                )
                if not (
                    batch.native_close_reconciliation_count==1
                    and len(due)==1
                    and closed.status in (
                        "V40_PORTFOLIO_CLOSE_EXCHANGE_TP_LABEL_CONFLICT_UNRELEASED",
                        "V40_PORTFOLIO_CLOSE_EXCHANGE_SL_LABEL_OBSERVED_UNRELEASED",
                    )
                    and closed.closed_position_id==due[0].position_id
                    and closed.exact_closed_position_history_verified is True
                    and closed.portfolio_unchanged_across_bookends is True
                    and closed.surviving_protected_slots==slots-1
                    and closed.closing_fills is not None
                    and closed.closing_fills.position_id==due[0].position_id
                    and closed.closing_fills.symbol==due[0].symbol
                    and closed.closing_fills.side=="CLOSE_SHORT"
                    and closed.closing_fills.order_id==closed.exact_close_order_id
                    and closed.closing_fills.deal_volume==
                        next(p.contracts for p in frame.snapshot.recovery.recovered
                             if p.position_id==due[0].position_id)
                    and closed.closing_fills.final_order_fill_history_complete is True
                    and closed.closing_fills.strategy_fully_filled_order_complete is True
                    and closed.native_terminal is not None
                    and closed.native_terminal.native_stop_terminal_verified is True
                    and closed.native_terminal.linked_venue_close_filled is True
                    and closed.native_terminal.owned_position_closed_verified is True
                ):
                    return self._halt(
                        "V40_SUPERVISOR_NATIVE_EXIT_RECONCILE_DURABLE_RELEASE_REQUIRED",
                        slots=slots,
                    )
                # A verified exchange exit is READ-ONLY; the subsequent
                # release is a real DB mutation. An entry signal permit
                # cannot be inferred from a positive close witness.
                if not self._permit_valid(permit):
                    return self._halt(
                        "V40_SUPERVISOR_CLOSE_RELEASE_PERMIT_UNAVAILABLE",
                        slots=slots,reconcile=True,
                    )
                # The port independently verifies exact origin/position
                # scope and positive funding/accounting before the SQL CAS.
                try:
                    released=await self.ports.release_verified_native_close(
                        projection,witness=closed,permit=permit,
                    )
                except BaseException:
                    return self._halt(
                        "V40_SUPERVISOR_NATIVE_CLOSE_DB_OUTCOME_UNKNOWN_RECONCILE",
                        slots=slots,reconcile=True,
                    )
                if not (
                    type(released) is V40NativeCloseReleaseHandoffReport
                    and released.status==
                        "V40_CLOSE_HANDOFF_ONE_SLOT_DURABLY_RELEASED_NEW_GET_REQUIRED"
                    and released.closed_position_id==closed.closed_position_id
                    and released.exact_exchange_close_verified is True
                    and released.persisted_accounting_verified is True
                    and released.durable_slot_release_verified is True
                    and released.database_commit_may_have_occurred is True
                    and released.fresh_account_ledger_guardian_recheck_required is True
                    and released.next_entry_allowed is False
                    and released.live_authorization is False
                    and released.mexc_posts==0
                ):
                    return self._halt(
                        "V40_SUPERVISOR_NATIVE_CLOSE_RELEASE_UNVERIFIED_RECONCILE",
                        slots=slots,reconcile=True,
                    )
                return self._halt(
                    "V40_SUPERVISOR_ONE_CLOSED_SLOT_RELEASED_NEW_GET_REQUIRED",
                    slots=slots,reconcile=True,
                )
            if batch.proposed_stop_amendment_count:
                if not self._permit_valid(permit):
                    return self._halt("V40_SUPERVISOR_STOP_PERMIT_UNAVAILABLE", slots=slots)
                # The stop port must persist SUBMITTING before exactly one POST.
                # No automatic retry, even on an apparently failed HTTP call.
                try:
                    stop = await self.ports.tighten_one_stop(projection, permit=permit)
                except BaseException:
                    return self._halt(
                        "V40_SUPERVISOR_STOP_UNKNOWN_RECONCILE", slots=slots, stop=True,
                    )
                if (
                    type(stop) is not V40UnreleasedStopCycleResult
                    or stop.live_authorization is not False
                    or stop.new_entries_allowed is not False
                    or stop.orders_sent != 0
                ):
                    return self._halt(
                        "V40_SUPERVISOR_STOP_RESULT_INVALID_RECONCILE",
                        slots=slots, stop=True,
                    )
                return self._halt(
                    "V40_SUPERVISOR_STOP_GET_AND_CHECKPOINT_REQUIRED",
                    slots=slots,
                    stop=stop.stop_post_may_have_occurred
                         or stop.requires_independent_reconciliation,
                )
            if batch.unchanged_verified_stop_count != len(batch.slots):
                return self._halt("V40_SUPERVISOR_EXIT_POLICY_DRIFT", slots=slots)
            # Advancing a durable 30-minute cursor changes future model
            # decisions, even when no exchange POST is required.
            if not self._permit_valid(permit):
                return self._halt(
                    "V40_SUPERVISOR_CURSOR_PERMIT_UNAVAILABLE", slots=slots,
                )
            try:
                advanced = await self.ports.persist_unchanged_stop_checkpoint(
                    projection,permit=permit,
                )
            except BaseException:
                return self._halt(
                    "V40_SUPERVISOR_CURSOR_COMMIT_UNKNOWN", slots=slots, entry=True,
                )
            if (
                type(advanced) is not V40CursorAdvanceResult
                or advanced.status !=
                    "V40_UNCHANGED_NATIVE_STOP_BAR_CURSOR_COMMITTED"
                or advanced.durable_commit_verified is not True
                or advanced.stop_bookends_verified is not True
                or advanced.exchange_posts != 0
                or len(advanced.position_ids) != len(batch.slots)
                or set(advanced.position_ids) !=
                    {s.position_id for s in batch.slots}
            ):
                return self._halt(
                    "V40_SUPERVISOR_CURSOR_NOT_VERIFIED",
                    slots=slots, entry=True,
                )
            return self._halt(
                "V40_SUPERVISOR_BAR_COMMITTED_REOBSERVE", slots=slots,
            )
        if slots == 4:
            return self._halt("V40_SUPERVISOR_FOUR_SLOTS_OCCUPIED", slots=slots)
        if not frame.source.candidates:
            return self._halt("V40_SUPERVISOR_WAIT_GENUINE_STAGE2", slots=slots)
        held = (
            {p.symbol for p in frame.snapshot.recovery.recovered}
            if slots else set()
        )
        candidates = [x for x in frame.source.candidates if x.symbol not in held]
        if not candidates:
            return self._halt("V40_SUPERVISOR_DUPLICATE_SYMBOL_SKIP", slots=slots)
        # The frozen multi-slot reference orders same-bar candidates by
        # (admission_open_ts, signal_id), exactly as the restricted source
        # query (ORDER BY p2_at,id). Never choose a provider/SQL iteration
        # order or arbitrarily skip a first candidate. One fresh fenced
        # cycle is required for every subsequent candidate.
        frozen_order=sorted(
            candidates,key=lambda x:(x.admission_open_ts,x.signal_id),
        )
        if candidates!=frozen_order:
            return self._halt(
                "V40_SUPERVISOR_SAME_BAR_PRIORITY_UNVERIFIED", slots=slots,
            )
        signal = frozen_order[0]
        if not self._permit_valid(permit):
            return self._halt(
                "V40_SUPERVISOR_ENTRY_PERMIT_UNAVAILABLE", slots=slots,
                signal=signal.signal_id,
            )
        # The port must use the existing single irreversible fenced V40
        # source/reservation/claim/POST/GET/commit coordinator. It rechecks
        # this exact signal and account risk at the last possible boundary.
        try:
            report = await self.ports.enter_one(
                permit=permit,caps=self.caps,parity_report=self.parity,
                holder_id=self.holder,monitored_signal_id=signal.signal_id,
                monitored_symbol=signal.symbol,monitored_tier=signal.tier,
            )
        except BaseException:
            return self._halt(
                "V40_SUPERVISOR_ENTRY_UNKNOWN_RECONCILE", slots=slots,
                signal=signal.signal_id,entry=True,
            )
        if type(report) is not V40OneShotReport:
            return self._halt(
                "V40_SUPERVISOR_ENTRY_RESULT_INVALID", slots=slots,
                signal=signal.signal_id,entry=True,
            )
        if (
            report.status=="V40_ONE_PROTECTED_ENTRY_RECOVERED_NOT_AUTONOMOUS"
            and report.signal_id==signal.signal_id
            and report.independently_recovered_after_commit is True
            and report.reserved is True and report.staged is True
            and report.claim_may_exist is True and report.post_attempted is True
            and report.entry_allowed is False
            and report.live_authorization is False
        ):
            return V40PortfolioSupervisorReport(
                "V40_SUPERVISOR_ONE_PROTECTED_ENTRY_REOBSERVE",
                observed_slots=slots,signal_id=signal.signal_id,
                origin_may_have_mutated=True,
            )
        return self._halt(
            "V40_SUPERVISOR_ENTRY_NOT_VERIFIED_RECONCILE",
            slots=slots,signal=signal.signal_id,
            entry=report.reserved or report.staged or report.claim_may_exist
                  or report.post_attempted,
        )


async def run_v40_portfolio_supervisor(
    controller: V40PortfolioSupervisor, *,
    permit: LiveMutationPermit | None = None,
    pause: Callable[[float], Awaitable[Any]] = asyncio.sleep,
    interval_seconds: int = 15,
    max_cycles: int | None = None,
) -> V40PortfolioSupervisorReport:
    """Continuous worker loop; terminal halt on ANY unknown transition."""
    if (
        type(controller) is not V40PortfolioSupervisor
        or type(interval_seconds) is not int or not 3<=interval_seconds<=60
        or max_cycles is not None
        and (type(max_cycles) is not int or max_cycles<1)
    ):
        raise ValueError("V40_SUPERVISOR_LOOP_CONFIG_INVALID")
    repeat={
        "V40_SUPERVISOR_WAIT_GENUINE_STAGE2",
        "V40_SUPERVISOR_DUPLICATE_SYMBOL_SKIP",
        "V40_SUPERVISOR_FOUR_SLOTS_OCCUPIED",
        "V40_SUPERVISOR_BAR_COMMITTED_REOBSERVE",
        "V40_SUPERVISOR_ONE_PROTECTED_ENTRY_REOBSERVE",
    }
    count=0
    while True:
        result=await controller.run_one(permit=permit)
        count+=1
        if result.status not in repeat or result.must_reconcile_before_next_cycle:
            return result
        if max_cycles is not None and count>=max_cycles:
            return result
        await pause(float(interval_seconds))
