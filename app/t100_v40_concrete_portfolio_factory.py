"""Concrete, exact-account one-signal V40 0..4-slot production graph.

This is an *assembly*, not an alternate strategy, a test-order shortcut, or
a way to issue a fabricated permit. It binds the existing production
Stage2, independent account GET / ledger, protected-entry SQL, order
transport, stop journal and native amendments, completed-bar cursor,
positive native-close GET, fill/funding/PnL persistence, and atomic slot
release behind one portfolio supervisor.

A graph is built for a *specific frozen signal and frozen cross-leverage
plan*. A brand-new graph must be assembled from a fresh independent risk
frame for every distinct candidate. Reusing a single static order context
across two signals would silently trade with the wrong margins/quantity.

The independent risk witness and stop preflight are deliberately required
inputs; a FLAT-only witness must not be passed for already occupied slots.
No release constant changes, exchange POSTs, DB queries or credential loading
occur in this constructor. The live worker must still issue the real,
signal-bound production permit and supply a fresh executor leader fence.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any,Awaitable,Callable

from app.t100_live_execution import LiveCaps
from app.t100_live_four_slot_ledger_reader import PostgresFourSlotLedgerReader
from app.t100_live_four_slot_mexc_readonly import (
    FourSlotMexcReadVenue,FourSlotMexcCompleteness,
)
from app.t100_live_four_slot_snapshot_coordinator import (
    FourSlotSnapshotCoordinator,
)
from app.t100_live_four_slot_reservations import PostgresFourSlotReservations
from app.t100_live_four_slot_attempt_journal import PostgresFourSlotAttemptJournal
from app.t100_live_four_slot_dispatch_claims import PostgresFourSlotDispatchClaims
from app.t100_live_four_slot_dispatch_claim_reader import PostgresFourSlotDispatchClaimReader
from app.t100_live_leader_fencing import (
    LeaderLease,LeaderLeaseCoordinator,PostgresLeaderLeaseStore,
)
from app.t100_live_stop_journal import PostgresStopJournal
from app.t100_live_stop_amendment_coordinator import StopAmendmentLifecycleCoordinator
from app.t100_live_readonly_probe import ReadOnlyMexcProbe
from app.t100_live_position_accounting import PostgresFinalAccountingLedger
from app.t100_live_v40_entry_coordinator import V40OneShotCoordinator
from app.t100_live_v40_stage2_source import (
    PostgresV40Stage2Source,V40SourceCandidate,
)
from app.t100_live_v40_protected_commit import PostgresProtectedV40Commit
from app.t100_v40_guardian_post_gate import PostgresV40GuardianPostGate
from app.t100_v40_unreleased_nonflat_guardian_evidence import (
    PostgresV40NonflatGuardianEvidence,
)
from app.t100_v40_owned_completed_bar_reader import PostgresV40OwnedBarReader
from app.t100_v40_portfolio_order_transport import (
    V40PortfolioPostContext,V40FourSlotPostGuard,
    V40FourSlotMexcOrderTransport,
)
from app.t100_v40_portfolio_stop_supervisor_port import (
    V40FourSlotStopLifecyclePort,
)
from app.t100_v40_real_current_stop_preflight import V40RealCurrentStopPreflight
from app.t100_v40_unchanged_bar_commit_port import PostgresV40UnchangedBarCommit
from app.t100_v40_portfolio_positive_native_close_reader import (
    V40PositiveNativeCloseReadPort,
)
from app.t100_v40_native_close_mismatch_recovery import (
    V40NativeCloseMismatchRecovery,
)
from app.t100_v40_at_entry_contract_specs import (
    PostgresV40AtEntryContractSpecReader,
)
from app.t100_v40_portfolio_close_financial_reader import (
    V40NativeCloseFinancialReadPort,
)
from app.t100_v40_close_financial_artifact_staging import (
    V40CloseOnlyFinancialArtifactStaging,
)
from app.t100_v40_unreleased_verified_slot_release import (
    PostgresV40VerifiedSlotRelease,
)
from app.t100_v40_native_close_release_handoff import (
    V40NativeCloseReleaseHandoff,V40NativeCloseReleaseHandoffReport,
)
from app.t100_v40_forward_close_finalizer import (
    V40ForwardCloseFinalizer,
)
from app.t100_v40_full_portfolio_runtime import (
    V40LivePortfolioPorts,assemble_full_v40_supervisor,
)
from app.t100_v40_live_portfolio_supervisor import V40PortfolioSupervisor
from app.t100_v40_unreleased_verified_exit_handoff import V40VerifiedExitProjection
from app.t100_v40_unreleased_portfolio_native_close import V40PortfolioCloseWitness
from app.t100_live_authorization import LiveMutationPermit


class V40ConcreteAssemblyHalted(RuntimeError):
    """Stable nonsecret reason; no backend connection/credential in errors."""


@dataclass(frozen=True)
class V40ConcretePortfolioGraph:
    ports: V40LivePortfolioPorts
    supervisor: V40PortfolioSupervisor
    source: PostgresV40Stage2Source
    ledger: PostgresFourSlotLedgerReader
    venue: FourSlotMexcReadVenue
    snapshots: FourSlotSnapshotCoordinator
    leaders: LeaderLeaseCoordinator
    entry: V40OneShotCoordinator
    stop: V40FourSlotStopLifecyclePort
    cursor: PostgresV40UnchangedBarCommit
    positive_close: V40PositiveNativeCloseReadPort
    settlement: V40ForwardCloseFinalizer
    signal_id: int
    symbol: str
    # Assembly itself does not certify the live strategy or trade permission.
    live_authorization: bool=False
    exchange_posts: int=0
    database_writes: int=0


class V40CandleNativeReleaseBridge:
    """Only exchange-proven native closes may reach the real release writer.

    A candle high touching a stop is NOT evidence that MEXC filled that stop.
    Before any DB finalization, make a fresh full-account mismatch snapshot
    and enter the same fenced financial path as the intrabar-stop supervisor.
    A position still present at MEXC can never be treated as closed.
    """

    def __init__(
        self, *,snapshots:FourSlotSnapshotCoordinator,
        finalizer:V40ForwardCloseFinalizer,
        fresh_lease:Callable[[],Awaitable[LeaderLease]],
    )->None:
        if (
            type(snapshots) is not FourSlotSnapshotCoordinator
            or type(finalizer) is not V40ForwardCloseFinalizer
            or not callable(fresh_lease)
        ):
            raise V40ConcreteAssemblyHalted("V40_CANDLE_CLOSE_PROVENANCE_MISSING")
        self.snapshots=snapshots
        self.finalizer=finalizer
        self.fresh_lease=fresh_lease

    async def release(
        self, projection:V40VerifiedExitProjection,
        witness:V40PortfolioCloseWitness, *,
        permit:LiveMutationPermit,
    )->V40NativeCloseReleaseHandoffReport:
        # A production portfolio permit is mandatory for the legacy bar
        # path. This cannot be reached from the observer/read-only worker.
        from app.t100_live_authorization import (
            V40_PORTFOLIO_SCOPE,require_certified_transport_permit,
        )
        from app.t100_v40_forward_close_finalizer import V40ForwardCloseResult
        try:
            checked=require_certified_transport_permit(permit)
            if (
                checked.scope!=V40_PORTFOLIO_SCOPE
                or type(projection) is not V40VerifiedExitProjection
                or type(witness) is not V40PortfolioCloseWitness
                or witness.exact_closed_position_history_verified is not True
                or witness.slot_released is not False
            ):
                raise ValueError()
            snapshot=await self.snapshots.inspect()
            if snapshot.status!="UNOWNED_OR_MISSING_CHECKPOINTS":
                raise ValueError()
            lease=await self.fresh_lease()
            result=await self.finalizer.run_once(
                snapshot=snapshot,lease=lease,
            )
            if (
                type(result) is not V40ForwardCloseResult
                or result.status!=
                   "V40_FORWARD_NATIVE_CLOSE_DURABLY_ACCOUNTED_AND_SLOT_RELEASED_REOBSERVE"
                or result.position_id!=witness.closed_position_id
                or result.slot_release_committed is not True
                or result.post_release_new_account_get_required is not True
                or result.live_authorization is not False
                or result.entry_allowed is not False
            ):
                raise ValueError()
            return V40NativeCloseReleaseHandoffReport(
                status="V40_CLOSE_HANDOFF_ONE_SLOT_DURABLY_RELEASED_NEW_GET_REQUIRED",
                closed_position_id=result.position_id,
                exact_exchange_close_verified=True,
                persisted_accounting_verified=True,
                durable_slot_release_verified=True,
                database_commit_may_have_occurred=True,
            )
        except BaseException:
            return V40NativeCloseReleaseHandoffReport(
                "V40_CANDLE_CLOSE_NOT_PROVEN_OR_DB_OUTCOME_UNKNOWN_RECONCILE",
                database_commit_may_have_occurred=True,
            )


def assemble_concrete_v40_signal_graph(
    *,source_pool:Any,ledger_pool:Any,writer_pool:Any,
    client:Any,probe:ReadOnlyMexcProbe,public_get:Any,
    account_scope_sha256:str,signal:V40SourceCandidate,
    context:V40PortfolioPostContext,
    entry_risk_witness:Any,depth_witness:Any,
    current_stop_preflight:Any,
    caps:LiveCaps,parity_report:dict[str,Any],holder_id:str,
    now_ms:Callable[[],int],
) -> V40ConcretePortfolioGraph:
    """Construct all existing audited dependencies with real identities.

    No SQL connection is opened. No exchange endpoint is contacted.
    Callers must calculate the signal-specific context from the current
    independent CROSS account/tier frame, never a cached previous signal.
    The actual runtime continues to reject any uncertified release permit.
    """
    import re
    if (
        source_pool is None or ledger_pool is None or writer_pool is None
        or len({id(source_pool),id(ledger_pool),id(writer_pool)})!=3
        or any(not callable(getattr(pool,"acquire",None))
               for pool in (source_pool,ledger_pool,writer_pool))
        or not callable(getattr(writer_pool,"fetchrow",None))
        or type(probe) is not ReadOnlyMexcProbe
        or getattr(probe,"_client",None) is not client
        or not callable(getattr(client,"_private_get",None))
        or not callable(getattr(client,"_private_post",None))
        or not callable(getattr(public_get,"get",None))
        or type(signal) is not V40SourceCandidate
        or type(context) is not V40PortfolioPostContext
        or type(caps) is not LiveCaps
        or type(parity_report) is not dict
        or type(holder_id) is not str or not holder_id.strip()
        or type(account_scope_sha256) is not str
        or re.fullmatch(r"[a-f0-9]{64}",account_scope_sha256) is None
        or not callable(now_ms)
        or not callable(getattr(entry_risk_witness,"inspect",None))
        or not callable(getattr(depth_witness,"quote_exact_intent",None))
        or (current_stop_preflight is not None
            and type(current_stop_preflight) is not V40RealCurrentStopPreflight)
        or context.leverage_plan.signal_id!=signal.signal_id
        or context.leverage_plan.symbol!=signal.symbol
        or context.frozen_order_notional_usdt!=
           context.leverage_plan.exact_order_notional_usdt
        or not 0<=context.expected_occupied_slots<=3
        or len(context.expected_owned_position_ids)!=context.expected_occupied_slots
    ):
        raise V40ConcreteAssemblyHalted("V40_CONCRETE_GRAPH_ACCOUNT_SIGNAL_OR_ROLE_INVALID")
    # One signal, one immutable plan, one irreversible POST maximum. A new
    # candidate (or changed equity/slots) requires a completely new graph.
    source=PostgresV40Stage2Source(source_pool,restricted_source_reader=True)
    ledger=PostgresFourSlotLedgerReader(ledger_pool)
    venue=FourSlotMexcReadVenue(probe)
    completeness=FourSlotMexcCompleteness(probe,venue)
    snapshots=FourSlotSnapshotCoordinator(
        ledger=ledger,venue=venue,completeness=completeness,
    )
    flat_guard=PostgresV40GuardianPostGate(ledger_pool)
    nonflat_guard=PostgresV40NonflatGuardianEvidence(ledger_pool)
    bars=PostgresV40OwnedBarReader(
        scanner_pool=source_pool,runtime_pool=ledger_pool,
        public_get=public_get,
    )
    leaders=LeaderLeaseCoordinator(
        PostgresLeaderLeaseStore(writer_pool),
    )
    reservations=PostgresFourSlotReservations(writer_pool)
    journal=PostgresFourSlotAttemptJournal(writer_pool)
    claims=PostgresFourSlotDispatchClaims(writer_pool)
    claim_reader=PostgresFourSlotDispatchClaimReader(ledger_pool)
    claimed_commit=PostgresProtectedV40Commit(writer_pool)
    post_guard=V40FourSlotPostGuard(
        ledger=ledger,client=client,public_get=public_get,
        flat_guard=flat_guard,owned_guard=nonflat_guard,
    )
    transport=V40FourSlotMexcOrderTransport(
        client=client,guard=post_guard,context=context,
    )
    entry=V40OneShotCoordinator(
        source=source,snapshots=snapshots,
        witness=entry_risk_witness,depth=depth_witness,
        leaders=leaders,reservations=reservations,
        journal=journal,claims=claims,claim_reader=claim_reader,
        transport=transport,get_venue=venue,
        completeness=completeness,commit_writer=claimed_commit,
        clock_ms=now_ms,initial_pilot_only=False,
    )
    if current_stop_preflight is None:
        current_stop_preflight=V40RealCurrentStopPreflight(
            ledger=ledger,snapshots=snapshots,venue=venue,
            completeness=completeness,owned_bars=bars,
            public_get=public_get,now_ms=now_ms,
        )
    stop=V40FourSlotStopLifecyclePort(
        client=client,ledger=ledger,
        guardian_reader=nonflat_guard,
        stop_lifecycle=StopAmendmentLifecycleCoordinator(
            PostgresStopJournal(writer_pool),
        ),
        preflight=current_stop_preflight,
    )
    close=V40PositiveNativeCloseReadPort(
        probe=probe,ledger=ledger,
        independently_verified_account_scope_sha256=account_scope_sha256,
    )
    archive=PostgresV40AtEntryContractSpecReader(ledger_pool)
    financial_reader=V40NativeCloseFinancialReadPort(
        probe=probe,public_get=public_get,entry_contracts=archive,
    )
    native=V40NativeCloseMismatchRecovery(
        ledger=ledger,venue=venue,probe=probe,
        independently_verified_account_scope_sha256=account_scope_sha256,
    )
    staged=V40CloseOnlyFinancialArtifactStaging(
        native=native,finance=financial_reader,
        ledger=ledger,writer_pool=writer_pool,
    )
    handoff=V40NativeCloseReleaseHandoff(
        probe=probe,writer=PostgresV40VerifiedSlotRelease(writer_pool),
    )
    settled=V40ForwardCloseFinalizer(
        staging=staged,final_accounting=PostgresFinalAccountingLedger(writer_pool),
        handoff=handoff,
    )
    # The cursor obtains independent fresh account/bookend and leader on
    # every action, never reuses the frame that initially proposed a stop.
    cell:dict[str,V40LivePortfolioPorts]={}
    async def fresh_observe():
        if "ports" not in cell:
            raise V40ConcreteAssemblyHalted("V40_CYCLE_NOT_READY")
        frame=await cell["ports"].observe()
        from app.t100_v40_live_portfolio_supervisor import V40PortfolioCycleFrame
        if type(frame) is not V40PortfolioCycleFrame:
            raise V40ConcreteAssemblyHalted("V40_FRESH_BAR_FRAME_NOT_OPEN")
        return frame
    async def fresh_lease():
        return await leaders.acquire(holder_id)
    cursor=PostgresV40UnchangedBarCommit(
        writer_pool=writer_pool,reader=ledger,venue=venue,
        fresh_observe=fresh_observe,fresh_lease=fresh_lease,
    )
    release=V40CandleNativeReleaseBridge(
        snapshots=snapshots,finalizer=settled,fresh_lease=fresh_lease,
    )
    ports=V40LivePortfolioPorts(
        source=source,ledger=ledger,snapshots=snapshots,
        venue=venue,completeness=completeness,
        flat_guard=flat_guard,nonflat_guard=nonflat_guard,
        owned_bars=bars,entry=entry,stop=stop,
        cursor=cursor,close=close,release=release,
        financial_staging=staged,forward_close=settled,
    )
    cell["ports"]=ports
    supervisor=assemble_full_v40_supervisor(
        ports,caps=caps,parity_report=parity_report,holder_id=holder_id,
    )
    return V40ConcretePortfolioGraph(
        ports=ports,supervisor=supervisor,source=source,ledger=ledger,
        venue=venue,snapshots=snapshots,leaders=leaders,
        entry=entry,stop=stop,cursor=cursor,
        positive_close=close,settlement=settled,
        signal_id=signal.signal_id,symbol=signal.symbol,
    )
