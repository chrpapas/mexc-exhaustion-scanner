"""Actual V40 four-slot supervisor adapter for a single fenced runtime.

This is the missing composition boundary between:
  signed account/stop/ledger + completed Min30 observations,
  the original irrevocable Stage2 coordinator,
  durable four-slot native stop management,
  immutable unchanged-bar cursor CAS,
  positive native-close/fill/funding reconciliation,
  one-slot atomic close/intent/reservation release.

The last three mutation providers must be concrete independent audited
implementations, not optional noops. Neither this module nor an env flag
creates a live permit, disables the kill switch, places an exchange order,
or changes the certified release constants.

The wrapper MUST NOT be used to paper over gaps in positive native-close
accounting or completion readbacks. Every port is checked before running.
"""
from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import datetime,timezone
from typing import Any, Protocol

from app.t100_live_authorization import (
    LiveMutationPermit, V40_PORTFOLIO_SCOPE,
    require_certified_transport_permit,
)
from app.t100_live_execution import LiveCaps
from app.t100_live_four_slot_ledger_reader import PostgresFourSlotLedgerReader
from app.t100_live_four_slot_mexc_readonly import (
    FourSlotMexcReadVenue,FourSlotMexcCompleteness,
)
from app.t100_live_four_slot_snapshot_coordinator import (
    FourSlotSnapshotCoordinator,
)
from app.t100_live_v40_entry_coordinator import V40OneShotCoordinator
from app.t100_live_v40_stage2_source import PostgresV40Stage2Source
from app.t100_v40_guardian_post_gate import PostgresV40GuardianPostGate
from app.t100_v40_unreleased_nonflat_guardian_evidence import (
    PostgresV40NonflatGuardianEvidence,
)
from app.t100_v40_nonflat_exit_frame_reader import V40RealNonflatExitFrameReader
from app.t100_v40_owned_completed_bar_reader import PostgresV40OwnedBarReader
from app.t100_v40_portfolio_stop_supervisor_port import (
    V40FourSlotStopLifecyclePort,
)
from app.t100_v40_unreleased_portfolio_native_close import V40PortfolioCloseWitness
from app.t100_v40_native_close_release_handoff import (
    V40NativeCloseReleaseHandoffReport,
)
from app.t100_v40_unreleased_verified_exit_handoff import V40VerifiedExitProjection
from app.t100_v40_live_portfolio_supervisor import (
    V40PortfolioCycleFrame,V40PortfolioSupervisor,
    V40PortfolioSupervisorReport,V40CursorAdvanceResult,
)
from app.t100_v40_unreleased_native_stop_cycle import V40UnreleasedStopCycleResult

class V40RuntimeAssemblyHalted(RuntimeError):
    """Stable, nonsecret failure: no silent fallback to a canary."""

class V40UnchangedBarCommitPort(Protocol):
    async def commit(
        self, projection: V40VerifiedExitProjection, *,
        permit: LiveMutationPermit,
    )->V40CursorAdvanceResult: ...

class V40NativeCloseWitnessPort(Protocol):
    async def observe(
        self, projection: V40VerifiedExitProjection,
    )->V40PortfolioCloseWitness: ...

class V40NativeCloseReleasePort(Protocol):
    async def release(
        self, projection: V40VerifiedExitProjection,
        witness: V40PortfolioCloseWitness, *,
        permit: LiveMutationPermit,
    )->V40NativeCloseReleaseHandoffReport: ...

@dataclass(frozen=True)
class V40RuntimePortAvailability:
    status: str
    entry_coordinator_connected: bool
    stop_mutation_connected: bool
    cursor_commit_connected: bool
    positive_close_readback_connected: bool
    final_funding_release_connected: bool
    ready_to_create_live_permit: bool=False
    live_authorization: bool=False
    mexc_posts: int=0
    database_writes: int=0


class V40LivePortfolioPorts:
    """Real-source 0..4-slot observation and typed action dispatch boundary.

    The supported helpers are injected with the *same* exchange signer and
    exact dedicated SQL roles by a separate production factory. Neither an
    API key nor a DB URL is loaded or logged in this class.
    """
    def __init__(
        self, *,
        source:PostgresV40Stage2Source,
        ledger:PostgresFourSlotLedgerReader,
        snapshots:FourSlotSnapshotCoordinator,
        venue:FourSlotMexcReadVenue,
        completeness:FourSlotMexcCompleteness,
        flat_guard:PostgresV40GuardianPostGate,
        nonflat_guard:PostgresV40NonflatGuardianEvidence,
        owned_bars:PostgresV40OwnedBarReader,
        entry:V40OneShotCoordinator,
        stop:V40FourSlotStopLifecyclePort,
        cursor:V40UnchangedBarCommitPort|None,
        close:V40NativeCloseWitnessPort|None,
        release:V40NativeCloseReleasePort|None,
    )->None:
        # Do not accept an incomplete production lifecycle. A missing SQL
        # cursor commit is as consequential as a missing exchange stop POST;
        # without positive final funding there is NO slot recycle.
        if (
            type(source) is not PostgresV40Stage2Source
            or type(ledger) is not PostgresFourSlotLedgerReader
            or type(snapshots) is not FourSlotSnapshotCoordinator
            or type(venue) is not FourSlotMexcReadVenue
            or type(completeness) is not FourSlotMexcCompleteness
            or type(flat_guard) is not PostgresV40GuardianPostGate
            or type(nonflat_guard) is not PostgresV40NonflatGuardianEvidence
            or type(owned_bars) is not PostgresV40OwnedBarReader
            or type(entry) is not V40OneShotCoordinator
            or type(stop) is not V40FourSlotStopLifecyclePort
            or entry.initial_pilot_only is not False
            or entry.source is not source or entry.snapshots is not snapshots
            or entry.get_venue is not venue
            or entry.completeness is not completeness
            or snapshots.ledger is not ledger
            or snapshots.venue is not venue
            or snapshots.completeness is not completeness
            or completeness.venue is not venue
            or stop.ledger is not ledger
            or flat_guard.pool is not ledger.pool
            or nonflat_guard.pool is not ledger.pool
            or owned_bars.runtime_pool is not ledger.pool
            or owned_bars.scanner_pool is not source.pool
            or (not callable(getattr(cursor,"commit",None)))
            or (not callable(getattr(close,"observe",None)))
            or (not callable(getattr(release,"release",None)))
        ):
            raise V40RuntimeAssemblyHalted(
                "V40_FULL_FOUR_SLOT_RUNTIME_PORTS_MISSING_OR_DIFFERENT_ACCOUNT"
            )
        # Reject disabled/canary or no-POST transport as the supposed live
        # four-slot executable.
        from app.t100_v40_portfolio_order_transport import V40FourSlotMexcOrderTransport
        if (
            type(entry.transport) is not V40FourSlotMexcOrderTransport
            or entry.transport.client is not stop.client
            or entry.transport.guard.client is not stop.client
            or entry.transport.guard.ledger is not ledger
        ):
            raise V40RuntimeAssemblyHalted(
                "V40_FULL_PORTFOLIO_TYPED_SAME_ACCOUNT_POST_TRANSPORT_REQUIRED"
            )
        self.source=source
        self.ledger=ledger
        self.snapshots=snapshots
        self.venue=venue
        self.flat_guard=flat_guard
        self.owned_bars=owned_bars
        self.nonflat_guard=nonflat_guard
        self.entry=entry
        self.stop=stop
        self.cursor=cursor
        self.close=close
        self.release=release
        self._action=asyncio.Lock()

    async def observe(self)->V40PortfolioCycleFrame:
        try:
            snap=await self.snapshots.inspect()
            now=datetime.now(timezone.utc)
            if snap.status=="FLAT_OBSERVED_NONAUTHORIZING":
                gate=await self.flat_guard.verify()
                batch=await self.source.latest_candidates(observed_at=now)
                return V40PortfolioCycleFrame(
                    snapshot=snap,guardian=gate,source=batch,
                    completed_exit=None,no_bar_due_ids=(),
                )
            if snap.status=="NONFLAT_PROVENANCE_MATCHED_NONAUTHORIZING":
                return await V40RealNonflatExitFrameReader(
                    owned_bars=self.owned_bars,
                    nonflat_guardian=self.nonflat_guard,
                ).observe(
                    snap=snap,venue=self.venue,ledger=self.ledger,
                    source=self.source,observed_at=now,
                )
            raise V40RuntimeAssemblyHalted(
                "V40_FULL_RUNTIME_UNVERIFIED_ACCOUNT_INVENTORY"
            )
        except V40RuntimeAssemblyHalted:
            raise
        except Exception:
            raise V40RuntimeAssemblyHalted(
                "V40_FULL_RUNTIME_SIGNED_GET_OR_RESTRICTED_LEDGER_HALT"
            ) from None

    @staticmethod
    def _check(permit:LiveMutationPermit)->None:
        try:
            checked=require_certified_transport_permit(permit)
            if checked.scope!=V40_PORTFOLIO_SCOPE:
                raise ValueError()
        except Exception:
            raise V40RuntimeAssemblyHalted(
                "V40_FULL_RUNTIME_PORTFOLIO_PERMIT_NOT_CERTIFIED"
            ) from None

    async def enter_one(self, *, permit:LiveMutationPermit, **args:Any)->Any:
        self._check(permit)
        if self._action.locked():
            raise V40RuntimeAssemblyHalted("V40_FULL_RUNTIME_CONCURRENT_MUTATION")
        async with self._action:
            return await self.entry.run_one(permit=permit,**args)

    async def tighten_one_stop(
        self, projection:V40VerifiedExitProjection, *,
        permit:LiveMutationPermit,
    )->V40UnreleasedStopCycleResult:
        self._check(permit)
        if self._action.locked():
            raise V40RuntimeAssemblyHalted("V40_FULL_RUNTIME_CONCURRENT_MUTATION")
        async with self._action:
            return await self.stop.tighten_one_stop(projection,permit=permit)

    async def persist_unchanged_stop_checkpoint(
        self, projection:V40VerifiedExitProjection, *,
        permit:LiveMutationPermit,
    )->V40CursorAdvanceResult:
        self._check(permit)
        if self._action.locked():
            raise V40RuntimeAssemblyHalted("V40_FULL_RUNTIME_CONCURRENT_MUTATION")
        async with self._action:
            return await self.cursor.commit(projection,permit=permit)

    async def reconcile_native_close(
        self, projection:V40VerifiedExitProjection,
    )->V40PortfolioCloseWitness:
        # Read-only native terminal proof can be obtained without permit,
        # but this method must not persist accounting or release a slot.
        return await self.close.observe(projection)

    async def release_verified_native_close(
        self, projection:V40VerifiedExitProjection,
        witness:V40PortfolioCloseWitness, *,
        permit:LiveMutationPermit,
    )->V40NativeCloseReleaseHandoffReport:
        self._check(permit)
        if self._action.locked():
            raise V40RuntimeAssemblyHalted("V40_FULL_RUNTIME_CONCURRENT_MUTATION")
        async with self._action:
            return await self.release.release(
                projection,witness,permit=permit,
            )


def assemble_full_v40_supervisor(
    ports:V40LivePortfolioPorts, *,
    caps:LiveCaps,parity_report:dict[str,Any],holder_id:str,
)->V40PortfolioSupervisor:
    if type(ports) is not V40LivePortfolioPorts:
        raise V40RuntimeAssemblyHalted(
            "V40_FULL_RUNTIME_ALL_FIVE_CONCRETE_PORTS_REQUIRED"
        )
    if caps.risk_profile!=V40_PORTFOLIO_SCOPE:
        raise V40RuntimeAssemblyHalted(
            "V40_FULL_RUNTIME_CANNOT_ADOPT_SINGLE_CANARY_CAPS"
        )
    return V40PortfolioSupervisor(
        ports,caps=caps,parity_report=parity_report,holder_id=holder_id,
    )
