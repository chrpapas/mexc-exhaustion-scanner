"""Offline V40 executor assembly contract: zero network, SQL and MEXC POST.

This constructs the complete dependency graph for the first tiny pilot,
but the shipped production permit/adapter gate still prevents execution.
No fixtures are traded, and no credentials or database URLs are logged.
"""
from __future__ import annotations

import asyncio
from decimal import Decimal as D
from types import SimpleNamespace

import pytest

from app.t100_live_authorization import LiveAuthorizationHalted
from app.t100_live_execution import LiveCaps
from app.t100_live_four_slot_attempt_journal import PostgresFourSlotAttemptJournal
from app.t100_live_four_slot_dispatch_claim_reader import PostgresFourSlotDispatchClaimReader
from app.t100_live_four_slot_dispatch_claims import PostgresFourSlotDispatchClaims
from app.t100_live_four_slot_ledger_reader import PostgresFourSlotLedgerReader
from app.t100_live_four_slot_reservations import PostgresFourSlotReservations
from app.t100_live_leader_fencing import LeaderLeaseCoordinator,PostgresLeaderLeaseStore
from app.t100_live_v40_protected_commit import PostgresProtectedV40Commit
from app.t100_live_v40_stage2_source import PostgresV40Stage2Source
from app.t100_live_guardian_readonly_once import GuardianOnceHalted

import app.t100_v40_executor_assembly as m


class NoNetworkHttp:
    base_url="https://api.mexc.com/"
    def __init__(self):
        self.calls=[]
    async def get(self,*args,**kwargs):
        self.calls.append(("GET",args,kwargs))
        return SimpleNamespace(
            raise_for_status=lambda:None,
            json=lambda:{"success":True,"code":0,"data":[]},
        )


class NoSql:
    def __init__(self):
        self.calls=[]
    def __getattr__(self,key):
        self.calls.append(key)
        raise AssertionError("NO_DATABASE_IO_IN_ASSEMBLY")


def assemble(**changes):
    signal=NoSql()
    runtime=NoSql()
    signed_http=NoNetworkHttp()
    public=NoNetworkHttp()
    signed=m.V40FirstPilotGetOnly(
        signed_http,api_key="testing-only-key-12345678",
        api_secret="testing-only-secret-12345678",
    )
    kw=dict(
        signal_reader_pool=signal,
        executor_runtime_pool=runtime,
        signed_get=signed,
        public_get=public,
        clock_ms=lambda:1234567890,
    )
    kw.update(changes)
    return m.assemble_v40_first_pilot(**kw),signal,runtime,signed_http,public


def test_wires_real_stage2_market_slot_claim_and_commit_types_without_io():
    report,source_pool,runtime_pool,http,public=assemble()
    c=report.coordinator
    assert type(c.source) is PostgresV40Stage2Source
    assert c.source.restricted_source_reader is True
    assert c.source.pool is source_pool
    assert c.initial_pilot_only is True
    assert type(c.snapshots.ledger) is PostgresFourSlotLedgerReader
    assert c.snapshots.ledger.pool is runtime_pool
    assert type(c.leaders) is LeaderLeaseCoordinator
    assert type(c.leaders.store) is PostgresLeaderLeaseStore
    assert type(c.reservations) is PostgresFourSlotReservations
    assert type(c.journal) is PostgresFourSlotAttemptJournal
    assert type(c.claims) is PostgresFourSlotDispatchClaims
    assert type(c.claim_reader) is PostgresFourSlotDispatchClaimReader
    assert type(c.commit_writer) is PostgresProtectedV40Commit
    assert c.depth is c.witness
    assert c.get_venue is c.snapshots.venue
    assert c.completeness is c.snapshots.completeness
    assert c.transport.open_type==2
    assert type(c.transport) is m.DisabledV40OrderSink
    assert not hasattr(c.transport,"_private_post")
    assert c.source.pool is not c.snapshots.ledger.pool
    assert not report.production_post_adapter_connected
    assert report.initial_position_capacity==1
    assert not report.entry_allowed and not report.live_authorization
    assert report.mexc_posts==0 and report.database_writes==0
    assert source_pool.calls==runtime_pool.calls==http.calls==public.calls==[]


def test_uncertified_permit_fails_before_stage2_or_any_db_access():
    report,source_pool,runtime_pool,http,public=assemble()
    with pytest.raises(LiveAuthorizationHalted,match="PERMIT_REQUIRED"):
        asyncio.run(report.coordinator.run_one(
            permit=None,
            caps=LiveCaps(
                max_order_usdt=D("12.50"),max_portfolio_usdt=D("12.50"),
                max_daily_loss_usdt=D("12.50"),
            ),
            parity_report={},holder_id="offline-fixture",
        ))
    assert source_pool.calls==runtime_pool.calls==http.calls==public.calls==[]


def test_disabled_order_transport_never_calls_exchange():
    report,_,_,http,_=assemble()
    with pytest.raises(m.V40AssemblyHalted,match="NO_POST"):
        asyncio.run(report.coordinator.transport.submit_short_with_stop(
            None,permit=None,
        ))
    assert http.calls==[]


@pytest.mark.parametrize("fault",[
    {"signal_reader_pool":None},
    {"executor_runtime_pool":None},
    {"signed_get":None},
    {"public_get":None},
    {"clock_ms":123},
    {"live_transport":object()},
    {"permit":object()},
])
def test_fail_closed_for_missing_roles_or_uncertified_adapter(fault):
    with pytest.raises((m.V40AssemblyHalted,LiveAuthorizationHalted)):
        assemble(**fault)


def test_identical_role_pool_is_denied():
    common=NoSql()
    with pytest.raises(m.V40AssemblyHalted,match="SEPARATE_ROLES"):
        assemble(signal_reader_pool=common,executor_runtime_pool=common)


def test_restricted_mexc_get_path_allowlist_no_mutations():
    report,_,_,signed_http,_=assemble()
    get=report.coordinator.get_venue.probe._client
    assert type(get) is m.V40FirstPilotGetOnly
    assert not hasattr(get,"_private_post")
    assert get._allowed("/api/v1/private/account/assets")
    assert get._allowed("/api/v1/private/position/position_mode")
    assert get._allowed("/api/v1/private/account/risk_limit")
    assert get._allowed("/api/v1/private/position/leverage")
    assert get._allowed("/api/v1/private/order/get/123")
    assert get._allowed("/api/v1/private/order/deal_details/123")
    for path in (
        "/api/v1/private/order/create",
        "/api/v1/private/stoporder/place",
        "/api/v1/private/stoporder/change_plan_price",
        "/api/v1/private/order/deal_details/123/../create",
    ):
        assert get._allowed(path) is False
        with pytest.raises(GuardianOnceHalted):
            asyncio.run(get._private_get(path))
    assert signed_http.calls==[]


@pytest.mark.parametrize("params",[
    None,{},{"symbol":"btc_usdt"},{"symbol":"BTC_USDT","other":1},
    {"symbol":"../BTC_USDT"},{"symbol":"BTC_USDT","limit":100},
])
def test_risk_limit_and_leverage_require_exact_symbol(params):
    _,_,_,signed_http,_=assemble()
    get=m.V40FirstPilotGetOnly(
        signed_http,api_key="testing-only-key-12345678",
        api_secret="testing-only-secret-12345678",
    )
    for endpoint in (
        "/api/v1/private/account/risk_limit",
        "/api/v1/private/position/leverage",
    ):
        with pytest.raises(GuardianOnceHalted,match="SCOPE"):
            asyncio.run(get._private_get(endpoint,params))
    assert signed_http.calls==[]


def test_allowed_readonly_risk_limit_never_changes_account():
    report,_,_,signed_http,_=assemble()
    get=report.coordinator.get_venue.probe._client
    raw=asyncio.run(get._private_get(
        "/api/v1/private/account/risk_limit",{"symbol":"EXAMPLE_USDT"},
    ))
    assert raw==[]
    assert len(signed_http.calls)==1
    assert signed_http.calls[0][0]=="GET"
    assert signed_http.calls[0][1][0]=="/api/v1/private/account/risk_limit"


def test_never_accepts_unpinned_http_hosts_or_same_account_secret_through_url():
    report,_,_,_,_=assemble()
    assert report.status=="V40_FIRST_PILOT_ONE_SHOT_ASSEMBLED_NONAUTHORIZING"
    evil=NoNetworkHttp()
    evil.base_url="https://private.invalid/"
    with pytest.raises(m.V40AssemblyHalted,match="HOST_UNPINNED"):
        assemble(public_get=evil)
    with pytest.raises(m.V40AssemblyHalted,match="HOST_UNPINNED"):
        assemble(signed_get=m.V40FirstPilotGetOnly(
            evil,api_key="testing-only-key-12345678",
            api_secret="testing-only-secret-12345678",
        ))



def fixture_current_risk_witness(signed,public):
    """Same-account, zero-I/O constructor. Never assert readiness in tests."""
    from app.t100_live_readonly_probe import ReadOnlyMexcProbe
    from app.t100_live_v40_candidate_market_witness import (
        MexcV40CandidateMarketWitness,
    )
    from app.t100_v40_first_canary_risk_witness import (
        V40FirstCanaryRiskWitness,
    )
    market=MexcV40CandidateMarketWitness(
        private=ReadOnlyMexcProbe(signed),public=public,
        clock_ms=lambda:1234567890,
    )
    async def unverified():
        raise AssertionError("NO_CURRENT_RISK_PROOF_IN_OFFLINE_ASSEMBLY")
    return V40FirstCanaryRiskWitness(
        market=market,get_permissions=unverified,
        get_daily_loss=unverified,get_runtime_journal=unverified,
    )


class PoolNoCalls(NoSql):
    def acquire(self):
        raise AssertionError("NO_DATABASE_IO_IN_ASSEMBLY")


def test_offline_live_canary_assembly_requires_same_get_and_post_api_identity(
    monkeypatch,
):
    from app.mexc_trade import MexcTradeClient
    from app.t100_live_mexc_adapter import LiveMexcAdapter
    from app.t100_v40_guardian_post_gate import PostgresV40GuardianPostGate

    # Test-only permit bypass does not affect the shipped NOT_IMPLEMENTED gate.
    monkeypatch.setattr(m,"require_certified_transport_permit",lambda p:p)
    runtime_pool=PoolNoCalls()
    reporter,signal_pool,_,http,public=assemble(
        executor_runtime_pool=runtime_pool,
    )
    signed=reporter.coordinator.witness.private._client

    venue=object.__new__(MexcTradeClient)
    venue.api_key=signed._key
    venue.api_secret=signed._secret
    venue.client=SimpleNamespace(base_url="https://api.mexc.com/")
    gate=PostgresV40GuardianPostGate(runtime_pool)
    adapter=LiveMexcAdapter(venue,guardian_gate=gate)
    proof=SimpleNamespace(scope="V40_CANARY_ONE_SHOT")

    report,_,_,_,_=assemble(
        signal_reader_pool=signal_pool,
        executor_runtime_pool=runtime_pool,
        signed_get=signed,
        public_get=public,
        live_transport=adapter,permit=proof,
        risk_witness=fixture_current_risk_witness(signed,public),
    )
    assert report.production_post_adapter_connected
    assert report.coordinator.transport is adapter
    assert http.calls==public.calls==[]
    assert signal_pool.calls==runtime_pool.calls==[]

    venue.api_key="different-subaccount-key"
    with pytest.raises(m.V40AssemblyHalted,match="ACCOUNT_IDENTITY_INVALID"):
        assemble(
            signal_reader_pool=signal_pool,
            executor_runtime_pool=runtime_pool,
            signed_get=signed,
            public_get=public,
            live_transport=adapter,permit=proof,
        risk_witness=fixture_current_risk_witness(signed,public),
        )
    venue.api_key=signed._key
    venue.api_secret="different-subaccount-secret"
    with pytest.raises(m.V40AssemblyHalted,match="ACCOUNT_IDENTITY_INVALID"):
        assemble(
            signal_reader_pool=signal_pool,
            executor_runtime_pool=runtime_pool,
            signed_get=signed,
            public_get=public,
            live_transport=adapter,permit=proof,
        risk_witness=fixture_current_risk_witness(signed,public),
        )


def test_live_canary_assembly_cannot_use_unrelated_guardian_sql_pool(monkeypatch):
    from app.mexc_trade import MexcTradeClient
    from app.t100_live_mexc_adapter import LiveMexcAdapter
    from app.t100_v40_guardian_post_gate import PostgresV40GuardianPostGate

    monkeypatch.setattr(m,"require_certified_transport_permit",lambda p:p)
    runtime_pool=PoolNoCalls()
    old,signal_pool,_,_,public=assemble(
        executor_runtime_pool=runtime_pool,
    )
    signed=old.coordinator.witness.private._client
    venue=object.__new__(MexcTradeClient)
    venue.api_key=signed._key
    venue.api_secret=signed._secret
    venue.client=SimpleNamespace(base_url="https://api.mexc.com")
    adapter=LiveMexcAdapter(
        venue,guardian_gate=PostgresV40GuardianPostGate(PoolNoCalls()),
    )
    with pytest.raises(m.V40AssemblyHalted,match="GUARDIAN_OR_ACCOUNT"):
        assemble(
            signal_reader_pool=signal_pool,
            executor_runtime_pool=runtime_pool,
            signed_get=signed,
            public_get=public,
            live_transport=adapter,permit=SimpleNamespace(scope="test"),
            risk_witness=fixture_current_risk_witness(signed,public),
        )


def test_canary_live_transport_never_uses_default_nonauthorizing_risk_witness(monkeypatch):
    from app.mexc_trade import MexcTradeClient
    from app.t100_live_mexc_adapter import LiveMexcAdapter
    from app.t100_v40_guardian_post_gate import PostgresV40GuardianPostGate

    monkeypatch.setattr(m,"require_certified_transport_permit",lambda p:p)
    runtime=PoolNoCalls()
    base,signal,_,http,public=assemble(
        executor_runtime_pool=runtime,
    )
    signed=base.coordinator.witness.private._client
    venue=object.__new__(MexcTradeClient)
    venue.api_key=signed._key
    venue.api_secret=signed._secret
    venue.client=SimpleNamespace(base_url="https://api.mexc.com/")
    transport=LiveMexcAdapter(
        venue,guardian_gate=PostgresV40GuardianPostGate(runtime),
    )
    with pytest.raises(m.V40AssemblyHalted,match="CURRENT_RISK_WITNESS_REQUIRED"):
        assemble(
            signal_reader_pool=signal,executor_runtime_pool=runtime,
            signed_get=signed,public_get=public,
            live_transport=transport,permit=SimpleNamespace(scope="test"),
        )
    assert signal.calls==runtime.calls==http.calls==public.calls==[]
