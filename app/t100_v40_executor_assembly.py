"""Explicit composition of V40's first-position one-shot execution path.

This is a reviewed *assembly boundary*, not a new strategy, background
scheduler, credential loader, or live deployment. By default it constructs
the REAL restricted Stage2/SQL journal/leader/claim/market GET components
with an intentionally non-POST transport. No connection, lease, DB mutation
or exchange request occurs during assembly.

A separate independently issued live permit, certified native-stop adapter
and production safety review are required before the optional production
transport can even be inserted. The currently deployed
LIVE_ADAPTER_STATUS=NOT_IMPLEMENTED prevents that. Never modify the flag,
fake certification booleans, or infer a trading permit from this wiring.

The first pilot is FLAT -> at most one SHORT, CROSS 1x. Unknown DB or venue
responses remain manual-recovery-only in the existing coordinator.
"""
from __future__ import annotations

from dataclasses import dataclass
import re
import time
from typing import Any, Callable

from app.t100_live_authorization import (
    LiveMutationPermit, require_certified_transport_permit,
)
from app.t100_live_execution import EntryIntent
from app.t100_live_four_slot_attempt_journal import PostgresFourSlotAttemptJournal
from app.t100_live_four_slot_dispatch_claim_reader import (
    PostgresFourSlotDispatchClaimReader,
)
from app.t100_live_four_slot_dispatch_claims import PostgresFourSlotDispatchClaims
from app.t100_live_four_slot_ledger_reader import PostgresFourSlotLedgerReader
from app.t100_live_four_slot_mexc_readonly import (
    FourSlotMexcReadVenue, FourSlotMexcCompleteness,
)
from app.t100_live_four_slot_reservations import PostgresFourSlotReservations
from app.t100_live_four_slot_snapshot_coordinator import FourSlotSnapshotCoordinator
from app.t100_live_guardian_readonly_once import (
    GetOnlyMexcTransport, GuardianOnceHalted,
)
from app.t100_live_leader_fencing import (
    LeaderLeaseCoordinator, PostgresLeaderLeaseStore,
)
from app.t100_live_mexc_adapter import LiveMexcAdapter
from app.t100_live_readonly_probe import ReadOnlyMexcProbe
from app.t100_live_v40_candidate_market_witness import (
    MexcV40CandidateMarketWitness,
)
from app.t100_live_v40_entry_coordinator import V40OneShotCoordinator
from app.t100_live_v40_protected_commit import PostgresProtectedV40Commit
from app.t100_live_v40_stage2_source import PostgresV40Stage2Source


class V40AssemblyHalted(RuntimeError):
    """Redacted, stable configuration error; never a raw URL/credential."""


class V40FirstPilotGetOnly(GetOnlyMexcTransport):
    """Combined exact-origin + market witness MEXC REST GET allowlist.

    Does not expose a POST method. Credentialed HTTP remains pinned to the
    official MEXC API host by the caller, and arbitrary params/endpoints
    cannot become a market or account-configuration mutation.
    """

    _FIXED=GetOnlyMexcTransport._FIXED | frozenset({
        "/api/v1/private/account/assets",
        "/api/v1/private/position/position_mode",
    })
    _SCOPED=frozenset({
        "/api/v1/private/account/risk_limit",
        "/api/v1/private/position/leverage",
    })

    @classmethod
    def _allowed(cls,path:str)->bool:
        return (
            type(path) is str
            and (
                super()._allowed(path)
                or path in cls._SCOPED
            )
        )

    async def _private_get(
        self,path:str,params:dict[str,Any]|None=None,
    )->Any:
        if path in self._SCOPED and not (
            type(params) is dict
            and set(params)=={"symbol"}
            and type(params["symbol"]) is str
            and re.fullmatch(r"[A-Z0-9]+_USDT",params["symbol"])
        ):
            raise GuardianOnceHalted("V40_GET_SYMBOL_SCOPE_INVALID")
        if path in {
            "/api/v1/private/account/assets",
            "/api/v1/private/position/position_mode",
        } and params is not None:
            raise GuardianOnceHalted("V40_GET_ACCOUNT_SCOPE_INVALID")
        return await super()._private_get(path,params)


class DisabledV40OrderSink:
    """No POST capability. Never use this as proof of a native stop."""
    open_type=2

    async def submit_short_with_stop(
        self,intent:EntryIntent,*,permit:LiveMutationPermit,
    )->str:
        raise V40AssemblyHalted("V40_DISABLED_ORDER_SINK_NO_POST")


@dataclass(frozen=True)
class V40ExecutorAssembly:
    """Wired dependency graph; NOT a readiness certificate or live worker."""
    coordinator: V40OneShotCoordinator
    status: str="V40_FIRST_PILOT_ONE_SHOT_ASSEMBLED_NONAUTHORIZING"
    initial_position_capacity: int=1
    production_post_adapter_connected: bool=False
    independent_guardian_connected: bool=False
    native_stop_lifecycle_certified: bool=False
    cross_collateral_certified: bool=False
    daily_loss_economics_certified: bool=False
    read_only_witnesses_wired: bool=True
    durable_claim_writers_constructed: bool=True
    deployment_registered: bool=False
    database_writes: int=0
    mexc_posts: int=0
    entry_allowed: bool=False
    live_authorization: bool=False


def assemble_v40_first_pilot(
    *,
    signal_reader_pool:Any,
    executor_runtime_pool:Any,
    signed_get:V40FirstPilotGetOnly,
    public_get:Any,
    clock_ms:Callable[[],int]|None=None,
    live_transport:LiveMexcAdapter|None=None,
    permit:LiveMutationPermit|None=None,
) -> V40ExecutorAssembly:
    """No-I/O injection of *real* V40 dependencies with strict identity.

    Two separate DB role pools are deliberate: signal_reader_pool connects
    as t100_v40_signal_reader (SELECT-only Stage2), runtime_pool as the
    minimally privileged t100_live_executor_runtime. Individual existing
    readers recheck current_user/database/READ ONLY on every SELECT. The
    writer modules enforce immutable journal CAS and fencing on mutation.

    To replace the disabled sink, callers must first hold a REAL typed
    independently issued permit and an independently certified transport.
    An environment flag or a mock is NOT enough. This function never issues
    a permit or creates a POST-capable MEXC client from credentials.
    """
    if (live_transport is None)!=(permit is None):
        raise V40AssemblyHalted(
            "V40_LIVE_TRANSPORT_AND_PERMIT_MUST_BE_SUPPLIED_TOGETHER"
        )
    # Permit gate FIRST, before any DB, market or transport collaborator.
    if live_transport is not None:
        require_certified_transport_permit(permit)
        if type(live_transport) is not LiveMexcAdapter or live_transport.open_type!=2:
            raise V40AssemblyHalted("V40_NATIVE_CROSS_POST_ADAPTER_NOT_CERTIFIED")
        from app.t100_v40_guardian_post_gate import PostgresV40GuardianPostGate
        from app.mexc_trade import MexcTradeClient
        gate=live_transport.guardian_gate
        venue=live_transport.client
        if (
            type(gate) is not PostgresV40GuardianPostGate
            or gate.pool is not executor_runtime_pool
            or type(venue) is not MexcTradeClient
            or type(signed_get) is not V40FirstPilotGetOnly
            # The supposedly independent read-only GET and the POST must
            # address the EXACT same MEXC account. No cross-subaccount trade.
            or venue.api_key!=signed_get._key
            or venue.api_secret!=signed_get._secret
            or str(venue.client.base_url).rstrip("/")!="https://api.mexc.com"
        ):
            raise V40AssemblyHalted(
                "V40_LIVE_ADAPTER_GUARDIAN_OR_ACCOUNT_IDENTITY_INVALID"
            )
    if (
        signal_reader_pool is None or executor_runtime_pool is None
        or signal_reader_pool is executor_runtime_pool
        or type(signed_get) is not V40FirstPilotGetOnly
        or hasattr(signed_get,"_private_post")
        or public_get is None or not callable(getattr(public_get,"get",None))
        or clock_ms is not None and not callable(clock_ms)
    ):
        raise V40AssemblyHalted("V40_EXECUTOR_SEPARATE_ROLES_OR_READONLY_GET_REQUIRED")
    base=getattr(public_get,"base_url",None)
    if str(base).rstrip("/")!="https://api.mexc.com":
        raise V40AssemblyHalted("V40_EXECUTOR_PUBLIC_HOST_UNPINNED")
    http=getattr(signed_get,"_client",None)
    if str(getattr(http,"base_url",None)).rstrip("/")!="https://api.mexc.com":
        raise V40AssemblyHalted("V40_EXECUTOR_SIGNED_HOST_UNPINNED")
    clock=clock_ms if clock_ms is not None else lambda:int(time.time()*1000)

    probe=ReadOnlyMexcProbe(signed_get)
    venue=FourSlotMexcReadVenue(probe)
    completeness=FourSlotMexcCompleteness(probe,venue)
    market=MexcV40CandidateMarketWitness(
        private=probe,public=public_get,clock_ms=clock,
        max_book_age_ms=1500,max_cycle_ms=5000,
    )
    coordinator=V40OneShotCoordinator(
        source=PostgresV40Stage2Source(
            signal_reader_pool,restricted_source_reader=True,
        ),
        snapshots=FourSlotSnapshotCoordinator(
            ledger=PostgresFourSlotLedgerReader(executor_runtime_pool),
            venue=venue,completeness=completeness,
        ),
        witness=market,depth=market,
        leaders=LeaderLeaseCoordinator(
            PostgresLeaderLeaseStore(executor_runtime_pool),
        ),
        reservations=PostgresFourSlotReservations(executor_runtime_pool),
        journal=PostgresFourSlotAttemptJournal(executor_runtime_pool),
        claims=PostgresFourSlotDispatchClaims(executor_runtime_pool),
        claim_reader=PostgresFourSlotDispatchClaimReader(executor_runtime_pool),
        transport=(
            live_transport if live_transport is not None
            else DisabledV40OrderSink()
        ),
        get_venue=venue,completeness=completeness,
        commit_writer=PostgresProtectedV40Commit(executor_runtime_pool),
        clock_ms=clock,initial_pilot_only=True,
    )
    return V40ExecutorAssembly(
        coordinator=coordinator,
        production_post_adapter_connected=(live_transport is not None),
    )
