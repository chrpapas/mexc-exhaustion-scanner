"""Signal-independent V40 production account/stop/settlement runtime graph.

The existing concrete signal factory must bind a genuine Stage2 and its exact
lot-specific CROSS plan before it can construct a trade adapter. That is
correct for opening orders, but *wrong for supervising an already-open
account*: native stops may execute when no Stage2 exists.

Build ALL real account-lifecycle ports (signed full-account inventory,
guardians, immutable Min30 bars, native stop journal, bar cursor, positive
native close, actual deal/funding/P&L and fenced one-slot release) with one
MEXC signer and three independent restricted SQL connections. There is NO
entry coordinator in the idle graph. Neither this factory nor its supervisor
can send any OPEN order, even if passed a syntactically valid portfolio
permit: a signal-bound order context must be added by the separate concrete
signal factory after a new current real signal and account plan exist.

The graph may be inspected in GET-only mode. Its immutable typed port
identity is suitable for subsequent independent audited permission/release.
A constructed graph is NOT proof that venue exit execution or SQL writes
succeeded and does not grant any live authorization.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any,Callable
import re

from app.t100_live_execution import LiveCaps
from app.t100_live_readonly_probe import ReadOnlyMexcProbe
from app.t100_live_four_slot_ledger_reader import PostgresFourSlotLedgerReader
from app.t100_live_four_slot_mexc_readonly import (
    FourSlotMexcReadVenue,FourSlotMexcCompleteness,
)
from app.t100_live_four_slot_snapshot_coordinator import (
    FourSlotSnapshotCoordinator,
)
from app.t100_live_v40_stage2_source import PostgresV40Stage2Source
from app.t100_v40_guardian_post_gate import PostgresV40GuardianPostGate
from app.t100_v40_unreleased_nonflat_guardian_evidence import (
    PostgresV40NonflatGuardianEvidence,
)
from app.t100_v40_owned_completed_bar_reader import PostgresV40OwnedBarReader
from app.t100_live_leader_fencing import (
    LeaderLeaseCoordinator,PostgresLeaderLeaseStore,
)
from app.t100_live_stop_journal import PostgresStopJournal
from app.t100_live_stop_amendment_coordinator import StopAmendmentLifecycleCoordinator
from app.t100_v40_portfolio_stop_supervisor_port import V40FourSlotStopLifecyclePort
from app.t100_v40_real_current_stop_preflight import V40RealCurrentStopPreflight
from app.t100_v40_portfolio_positive_native_close_reader import (
    V40PositiveNativeCloseReadPort,
)
from app.t100_v40_native_close_mismatch_recovery import (
    V40NativeCloseMismatchRecovery,
)
from app.t100_v40_at_entry_contract_specs import PostgresV40AtEntryContractSpecReader
from app.t100_v40_portfolio_close_financial_reader import (
    V40NativeCloseFinancialReadPort,
)
from app.t100_v40_close_financial_artifact_staging import (
    V40CloseOnlyFinancialArtifactStaging,
)
from app.t100_v40_native_close_release_handoff import V40NativeCloseReleaseHandoff
from app.t100_v40_unreleased_verified_slot_release import (
    PostgresV40VerifiedSlotRelease,
)
from app.t100_live_position_accounting import PostgresFinalAccountingLedger
from app.t100_v40_forward_close_finalizer import V40ForwardCloseFinalizer
from app.t100_v40_concrete_portfolio_factory import (
    V40CandleNativeReleaseBridge,
)
from app.t100_v40_unchanged_bar_commit_port import PostgresV40UnchangedBarCommit
from app.t100_v40_full_portfolio_runtime import (
    V40LivePortfolioPorts,assemble_full_v40_supervisor,
)
from app.t100_v40_live_portfolio_supervisor import (
    V40PortfolioSupervisor,V40PortfolioCycleFrame,
)


class V40IdlePortfolioAssemblyHalted(RuntimeError):
    """No credentials, venue payloads or SQL contents in errors."""


@dataclass(frozen=True)
class V40ConcreteIdlePortfolioGraph:
    ports:V40LivePortfolioPorts
    supervisor:V40PortfolioSupervisor
    source:PostgresV40Stage2Source
    ledger:PostgresFourSlotLedgerReader
    venue:FourSlotMexcReadVenue
    snapshots:FourSlotSnapshotCoordinator
    leaders:LeaderLeaseCoordinator
    stop:V40FourSlotStopLifecyclePort
    cursor:PostgresV40UnchangedBarCommit
    positive_close:V40PositiveNativeCloseReadPort
    settlement:V40ForwardCloseFinalizer
    signal_specific_entry_bound:bool=False
    entry_allowed:bool=False
    live_authorization:bool=False
    exchange_posts:int=0
    database_writes:int=0


def assemble_concrete_v40_idle_portfolio_graph(
    *,source_pool:Any,ledger_pool:Any,writer_pool:Any,
    client:Any,probe:ReadOnlyMexcProbe,public_get:Any,
    account_scope_sha256:str,caps:LiveCaps,
    parity_report:dict[str,Any],holder_id:str,now_ms:Callable[[],int],
)->V40ConcreteIdlePortfolioGraph:
    """Construct real full exit lifecycle with no invented signal or lot."""
    if (
        source_pool is None or ledger_pool is None or writer_pool is None
        or len({id(source_pool),id(ledger_pool),id(writer_pool)})!=3
        or any(not callable(getattr(p,"acquire",None))
               for p in (source_pool,ledger_pool,writer_pool))
        or not callable(getattr(writer_pool,"fetchrow",None))
        or type(probe) is not ReadOnlyMexcProbe
        or getattr(probe,"_client",None) is not client
        or not callable(getattr(client,"_private_get",None))
        or not callable(getattr(client,"_private_post",None))
        or not callable(getattr(public_get,"get",None))
        or type(account_scope_sha256) is not str
        or re.fullmatch("[a-f0-9]{64}",account_scope_sha256) is None
        or type(caps) is not LiveCaps
        or type(parity_report) is not dict
        or type(holder_id) is not str or not holder_id.strip()
        or not callable(now_ms)
    ):
        raise V40IdlePortfolioAssemblyHalted(
            "V40_IDLE_PORTFOLIO_SQL_SIGNER_ACCOUNT_OR_CAPS_UNVERIFIED"
        )
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
    leaders=LeaderLeaseCoordinator(PostgresLeaderLeaseStore(writer_pool))
    stop_preflight=V40RealCurrentStopPreflight(
        ledger=ledger,snapshots=snapshots,
        venue=venue,completeness=completeness,
        owned_bars=bars,public_get=public_get,now_ms=now_ms,
    )
    stop=V40FourSlotStopLifecyclePort(
        client=client,ledger=ledger,
        guardian_reader=nonflat_guard,
        stop_lifecycle=StopAmendmentLifecycleCoordinator(
            PostgresStopJournal(writer_pool),
        ),
        preflight=stop_preflight,
    )
    close=V40PositiveNativeCloseReadPort(
        probe=probe,ledger=ledger,
        independently_verified_account_scope_sha256=account_scope_sha256,
    )
    native=V40NativeCloseMismatchRecovery(
        ledger=ledger,venue=venue,probe=probe,
        independently_verified_account_scope_sha256=account_scope_sha256,
    )
    finance=V40NativeCloseFinancialReadPort(
        probe=probe,public_get=public_get,
        entry_contracts=PostgresV40AtEntryContractSpecReader(ledger_pool),
    )
    staging=V40CloseOnlyFinancialArtifactStaging(
        native=native,finance=finance,
        ledger=ledger,writer_pool=writer_pool,
    )
    handoff=V40NativeCloseReleaseHandoff(
        probe=probe,writer=PostgresV40VerifiedSlotRelease(writer_pool),
    )
    finalizer=V40ForwardCloseFinalizer(
        staging=staging,
        final_accounting=PostgresFinalAccountingLedger(writer_pool),
        handoff=handoff,
    )
    # A fresh single-instance action uses its own real current leader.
    cell:dict[str,V40LivePortfolioPorts]={}
    async def fresh_observe():
        ports=cell.get("ports")
        if type(ports) is not V40LivePortfolioPorts:
            raise V40IdlePortfolioAssemblyHalted("V40_IDLE_FRESH_FRAME_NOT_BOUND")
        frame=await ports.observe()
        if type(frame) is not V40PortfolioCycleFrame:
            raise V40IdlePortfolioAssemblyHalted(
                "V40_IDLE_FRESH_ACCOUNT_CLOSED_OR_UNKNOWN"
            )
        return frame
    async def fresh_lease():
        return await leaders.acquire(holder_id)
    cursor=PostgresV40UnchangedBarCommit(
        writer_pool=writer_pool,reader=ledger,venue=venue,
        fresh_observe=fresh_observe,fresh_lease=fresh_lease,
    )
    release=V40CandleNativeReleaseBridge(
        snapshots=snapshots,finalizer=finalizer,fresh_lease=fresh_lease,
    )
    ports=V40LivePortfolioPorts(
        source=source,ledger=ledger,snapshots=snapshots,
        venue=venue,completeness=completeness,
        flat_guard=flat_guard,nonflat_guard=nonflat_guard,
        owned_bars=bars,entry=None,stop=stop,cursor=cursor,
        close=close,release=release,
        financial_staging=staging,forward_close=finalizer,
        signal_idle_entry_unbound=True,
    )
    cell["ports"]=ports
    supervisor=assemble_full_v40_supervisor(
        ports,caps=caps,parity_report=parity_report,holder_id=holder_id,
    )
    return V40ConcreteIdlePortfolioGraph(
        ports=ports,supervisor=supervisor,source=source,
        ledger=ledger,venue=venue,snapshots=snapshots,
        leaders=leaders,stop=stop,cursor=cursor,positive_close=close,
        settlement=finalizer,
    )
