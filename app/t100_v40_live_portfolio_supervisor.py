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
    async def observe(self) -> V40PortfolioCycleFrame: ...
    async def persist_unchanged_stop_checkpoint(
        self, projection: V40VerifiedExitProjection,
    ) -> V40CursorAdvanceResult: ...
    async def reconcile_native_close(
        self, projection: V40VerifiedExitProjection,
    ) -> V40PortfolioCloseWitness: ...
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
        entry: bool = False, stop: bool = False,
    ) -> V40PortfolioSupervisorReport:
        return V40PortfolioSupervisorReport(
            code, observed_slots=slots, signal_id=signal,
            origin_may_have_mutated=entry,
            stop_post_may_have_occurred=stop,
            must_reconcile_before_next_cycle=entry or stop,
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
            frame.no_bar_due_ids == ()
            and type(projection) is V40VerifiedExitProjection
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
            and len(projection.batch.slots) == len(ids)
            and {s.position_id for s in projection.batch.slots} == set(ids)
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
    ) -> V40PortfolioSupervisorReport:
        try:
            frame = await self.ports.observe()
        except Exception:
            return self._halt("V40_SUPERVISOR_SIGNED_GET_OR_SQL_HALT")
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
                return self._halt(
                    "V40_SUPERVISOR_NATIVE_EXIT_RECONCILE_DURABLE_RELEASE_REQUIRED",
                    slots=slots,
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
            if batch.unchanged_verified_stop_count != slots:
                return self._halt("V40_SUPERVISOR_EXIT_POLICY_DRIFT", slots=slots)
            try:
                advanced = await self.ports.persist_unchanged_stop_checkpoint(projection)
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
                or len(advanced.position_ids) != slots
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
        signal = candidates[0]
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
