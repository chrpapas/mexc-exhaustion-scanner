"""Offline four-slot market SHORT + attached native SL75 POST contract.

No real MEXC GET/POST or DB. Production release stays NOT_CERTIFIED; tests
patch permit release *only in process* and use fake signed GET evidence.
"""
from __future__ import annotations
import asyncio
from decimal import Decimal as D
from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import patch
import pytest

from app.t100_live_execution import EntryIntent,deterministic_t100_entry_oid
from app.t100_live_four_slot_ledger_reader import PostgresFourSlotLedgerReader
from app.t100_v40_guardian_post_gate import (
    PostgresV40GuardianPostGate,V40GuardianPostGateEvidence,
)
from app.t100_v40_unreleased_nonflat_guardian_evidence import (
    PostgresV40NonflatGuardianEvidence,V40NonflatGuardianEvidence,
)
from app.t100_v40_exchange_leverage_plan import (
    V40CrossLeverageInputs,assess_exact_v40_cross_leverage,
)
from app import t100_v40_portfolio_order_transport as m
from tests.test_t100_v40_live_portfolio_supervisor import clean_flat,active


class Client:
    def __init__(self):
        self.posts=[]
        self.gets=[]
        self.ack="739113577038255616"
        self.post_error=None
        self.assets=[{
            "currency":"USDT","equity":"100",
            "availableBalance":"60","cashBalance":"100",
            "unrealized":"0","positionMargin":"40",
            "debtAmount":0,"bonus":0,
        }]
        self.leverages=[{
            "positionType":2,"openType":2,"leverage":3,
            "maxLeverageView":20,
        }]
        self.risk={
            "SIREN_USDT":[{
                "positionType":2,"openType":2,"imr":"0.01",
                "mmr":"0.005","maxLeverage":20,
                "maxVol":1000,
            }]
        }
    async def _private_get(self,path,params=None):
        self.gets.append((path,params))
        if path=="/api/v1/private/account/assets":
            return [dict(x) for x in self.assets]
        if path=="/api/v1/private/position/leverage":
            assert params=={"symbol":"SIREN_USDT"}
            return [dict(x) for x in self.leverages]
        if path=="/api/v1/private/account/risk_limit":
            assert params=={"symbol":"SIREN_USDT"}
            return self.risk
        raise AssertionError("UNKNOWN_PRIVATE_GET")
    async def _private_post(self,path,payload):
        self.posts.append((path,payload))
        if self.post_error is not None:
            raise self.post_error
        return self.ack


class Response:
    def __init__(self,contract):self.contract=contract
    def raise_for_status(self):pass
    def json(self):return {"success":True,"data":self.contract}


class Public:
    def __init__(self):
        self.calls=[]
        self.contract=dict(
            symbol="SIREN_USDT",apiAllowed=True,state=0,
            positionOpenType=3,futureType=1,settleCoin="USDT",
            stopOnlyFair=False,maxLeverage=20,
            countryConfigContractMaxLeverage=0,
            volUnit="0.1",priceUnit="0.1",contractSize="1",
            minVol="0.1",maxVol="1000",
        )
    async def get(self,path,params):
        self.calls.append((path,params))
        assert path=="/api/v1/contract/detail/country"
        assert params=={"symbol":"SIREN_USDT"}
        return Response(dict(self.contract))


def planned(*,prior_gross="0"):
    prev=D(prior_gross)
    return assess_exact_v40_cross_leverage(
        V40CrossLeverageInputs(
            signal_id=497,symbol="SIREN_USDT",
            frozen_lot_notional_usdt=D("120"),
            executable_intent_notional_usdt=D("120"),
            current_gross_notional_usdt=prev,
            account_equity_usdt=D("100"),
            signed_available_initial_margin_usdt=D("60"),
            independent_fee_reserve_usdt=D("1"),
            required_remaining_margin_headroom_usdt=D("5"),
            observed_contract_max_cross_leverage=20,
            observed_account_max_cross_leverage=20,
            operator_max_cross_leverage=5,
            account_cross_mode_verified=True,
            account_identity_bookended_verified=True,
            available_margin_private_signed_get_verified=True,
            per_contract_and_private_leverage_tiers_verified=True,
            frozen_strategy_lot_and_signal_verified=True,
        )
    )


def intent():
    return EntryIntent(
        signal_id=497,symbol="SIREN_USDT",
        reference_price=D("100"),
        notional_usdt=D("120"),
        volume_contracts=D("1.2"),
        stop_price=D("175"),
        external_oid=deterministic_t100_entry_oid(497,"SIREN_USDT"),
    )


def permit():
    return SimpleNamespace(
        scope="T100_V40_FOUR_SLOT_PRODUCTION_TRIAL",
        signal_id=497,symbol="SIREN_USDT",
        leverage=3,
        max_position_notional_usdt=D("120"),
        max_total_notional_usdt=D("500"),
    )


def context(n=0,ids=()):
    plan=planned(prior_gross=str(n*50))
    assert plan.selected_exchange_cross_leverage==3
    return m.V40PortfolioPostContext(
        expected_occupied_slots=n,
        expected_owned_position_ids=tuple(ids),
        frozen_order_notional_usdt=D("120"),
        frozen_portfolio_gross_after_entry_usdt=D("120")+D(n)*D("50"),
        frozen_equity_at_plan_usdt=D("100"),
        protected_remaining_margin_headroom_usdt=D("5"),
        fee_reserve_usdt=D("1"),
        leverage_plan=plan,
    )


def gate(client,public):
    ledger=PostgresFourSlotLedgerReader.__new__(PostgresFourSlotLedgerReader)
    fg=PostgresV40GuardianPostGate.__new__(PostgresV40GuardianPostGate)
    og=PostgresV40NonflatGuardianEvidence.__new__(
        PostgresV40NonflatGuardianEvidence
    )
    async def flat():
        return V40GuardianPostGateEvidence(
            m.FLAT_OK,state_verified=True,
            outbox_verified=True,guardian_fresh=True,
            critical_latch_absent=True,
        )
    async def nonflat():
        return V40NonflatGuardianEvidence(
            m.NONFLAT_OK,
            guardian_protected_observation_fresh=True,
            no_current_scope_critical_or_pending_incidents=True,
        )
    fg.verify=flat
    og.verify=nonflat
    return m.V40FourSlotPostGuard(
        ledger=ledger,client=client,public_get=public,
        flat_guard=fg,owned_guard=og,
    )


async def setup(n):
    client=Client()
    public=Public()
    g=gate(client,public)
    if n:
        frame=active(n,with_candidate=False)
        snap=frame.snapshot
        ids=tuple(x.position_id for x in snap.recovery.recovered)
    else:
        snap=await clean_flat()
        ids=()
    times=0
    async def current_snapshot():
        nonlocal times
        times+=1
        return snap
    g._snapshot=current_snapshot
    context_now=context(n,ids)
    tr=m.V40FourSlotMexcOrderTransport(
        client=client,guard=g,context=context_now,
    )
    return client,public,g,tr,times


@pytest.mark.asyncio
async def test_unpatched_live_release_gate_refuses_before_signed_get_or_post():
    client,public,g,tr,times=await setup(0)
    with pytest.raises(m.V40PortfolioPostHalted,match="PERMIT_NOT_CERTIFIED_NO_POST"):
        await tr.submit_short_with_stop(intent(),permit=permit())
    assert not client.gets and not client.posts and not public.calls


@pytest.mark.asyncio
@pytest.mark.parametrize("n",[0,1,2,3])
async def test_four_slot_sequence_each_order_is_one_short_cross_with_sl75(n,monkeypatch):
    client,public,g,tr,times=await setup(n)
    monkeypatch.setattr(m,"require_certified_transport_permit",lambda p:p)
    oid=await tr.submit_short_with_stop(intent(),permit=permit())
    assert oid=="739113577038255616"
    assert client.posts==[(
        "/api/v1/private/order/create",
        dict(
            symbol="SIREN_USDT",price="100",vol="1.2",leverage=3,
            side=3,type=5,openType=2,positionMode=1,
            externalOid=intent().external_oid,
            stopLossPrice="175",lossTrend=1,
        ),
    )]
    assert len(client.gets)==4  # two asset GETs, private leverage and risk
    assert len(public.calls)==1
    assert tr._post_attempted is True
    # A second POST from the same object is never allowed, even after ACK.
    with pytest.raises(m.V40PortfolioPostHalted,match="ALREADY_POSTED_RECONCILE"):
        await tr.submit_short_with_stop(intent(),permit=permit())
    assert len(client.posts)==1


@pytest.mark.asyncio
async def test_four_slots_full_must_be_rejected_no_network(monkeypatch):
    client,public,g,tr,times=await setup(3)
    monkeypatch.setattr(m,"require_certified_transport_permit",lambda p:p)
    tr.context=replace(tr.context,expected_occupied_slots=4,
                       expected_owned_position_ids=("1","2","3","4"))
    with pytest.raises(m.V40PortfolioPostHalted,match="EXPECTED_SLOTS_INVALID"):
        await tr.submit_short_with_stop(intent(),permit=permit())
    assert not client.gets and not client.posts


@pytest.mark.asyncio
async def test_frozen_lot_or_signal_or_legacy_scope_invalid_before_any_get(monkeypatch):
    client,public,g,tr,times=await setup(0)
    monkeypatch.setattr(m,"require_certified_transport_permit",lambda p:p)
    for i in [
        replace(intent(),notional_usdt=D("12.50")),
        replace(intent(),stop_price=D("120")),
        replace(intent(),external_oid="reusedmanual"),
    ]:
        with pytest.raises(m.V40PortfolioPostHalted,match="EXACT_STRATEGY"):
            await tr.submit_short_with_stop(i,permit=permit())
    bad=permit()
    bad.scope="T100_V40_SUPERVISED_FIRST_SIGNAL"
    with pytest.raises(m.V40PortfolioPostHalted,match="SCOPE_OR_INTENT_INVALID"):
        await tr.submit_short_with_stop(intent(),permit=bad)
    assert not client.gets and not client.posts


@pytest.mark.asyncio
async def test_guardian_latch_blocks_flat_and_nonflat_before_post(monkeypatch):
    monkeypatch.setattr(m,"require_certified_transport_permit",lambda p:p)
    for n in (0,1,2,3):
        client,public,g,tr,times=await setup(n)
        if n==0:
            async def bad():
                return V40GuardianPostGateEvidence(
                    "V40_GUARDIAN_POST_GATE_LATCH_OR_FRESHNESS_HALT"
                )
            g.flat_guard.verify=bad
        else:
            async def bad():
                return V40NonflatGuardianEvidence(
                    "V40_NONFLAT_GUARDIAN_NOT_FRESH_OR_OWNED_HALT"
                )
            g.owned_guard.verify=bad
        with pytest.raises(m.V40PortfolioPostHalted,match="GUARDIAN_LATCHED_NO_POST"):
            await tr.submit_short_with_stop(intent(),permit=permit())
        assert client.posts==[]


@pytest.mark.asyncio
@pytest.mark.parametrize("case",[
    "unavailable","risklimit","nonapi","faironly","wrongvol",
    "wrongtick","wrongscope","tiermissing","leverage",
])
async def test_current_exchange_contract_and_margin_checks_reject_before_post(monkeypatch,case):
    client,public,g,tr,times=await setup(0)
    monkeypatch.setattr(m,"require_certified_transport_permit",lambda p:p)
    if case=="unavailable":
        client.assets[0]["availableBalance"]="39"
    if case=="risklimit":
        client.risk["SIREN_USDT"][0]["maxVol"]=1
    if case=="nonapi":
        public.contract["apiAllowed"]=False
    if case=="faironly":
        public.contract["stopOnlyFair"]=True
    if case=="wrongvol":
        public.contract["volUnit"]="0.7"
    if case=="wrongtick":
        public.contract["priceUnit"]="0.3"
    if case=="wrongscope":
        client.leverages[0]["openType"]=1
    if case=="tiermissing":
        client.risk["SIREN_USDT"]=[]
    if case=="leverage":
        client.risk["SIREN_USDT"][0]["maxLeverage"]=2
    with pytest.raises(m.V40PortfolioPostHalted):
        await tr.submit_short_with_stop(intent(),permit=permit())
    assert client.posts==[]


@pytest.mark.asyncio
async def test_nonflat_position_change_between_bookends_blocks_post(monkeypatch):
    client,public,g,tr,times=await setup(2)
    monkeypatch.setattr(m,"require_certified_transport_permit",lambda p:p)
    prev=await g._snapshot()
    changed=replace(prev,critical_inventory_hash="0"*64)
    checks=0
    async def drift():
        nonlocal checks
        checks+=1
        return prev if checks==1 else changed
    g._snapshot=drift
    with pytest.raises(m.V40PortfolioPostHalted,match="FINAL_BOOKEND_CHANGED_NO_POST"):
        await tr.submit_short_with_stop(intent(),permit=permit())
    assert client.posts==[] and checks==2


@pytest.mark.asyncio
async def test_guardian_latches_between_current_risk_and_post(monkeypatch):
    client,public,g,tr,times=await setup(1)
    monkeypatch.setattr(m,"require_certified_transport_permit",lambda p:p)
    count=0
    async def changed_guardian():
        nonlocal count
        count+=1
        if count==1:
            return V40NonflatGuardianEvidence(
                m.NONFLAT_OK,
                guardian_protected_observation_fresh=True,
                no_current_scope_critical_or_pending_incidents=True,
            )
        return V40NonflatGuardianEvidence("V40_GUARDIAN_CRITICAL_LATCHED")
    g.owned_guard.verify=changed_guardian
    with pytest.raises(m.V40PortfolioPostHalted,match="GUARDIAN_LATCHED_NO_POST"):
        await tr.submit_short_with_stop(intent(),permit=permit())
    assert count==2 and client.posts==[]


@pytest.mark.asyncio
async def test_ambiguous_post_no_retry_even_with_same_permit(monkeypatch):
    client,public,g,tr,times=await setup(0)
    monkeypatch.setattr(m,"require_certified_transport_permit",lambda p:p)
    client.post_error=TimeoutError("SECRET URL")
    with pytest.raises(m.V40PortfolioPostHalted,match="OUTCOME_UNKNOWN_RECONCILE_NO_RETRY"):
        await tr.submit_short_with_stop(intent(),permit=permit())
    with pytest.raises(m.V40PortfolioPostHalted,match="ALREADY_POSTED_RECONCILE_NO_RETRY"):
        await tr.submit_short_with_stop(intent(),permit=permit())
    assert len(client.posts)==1
    assert "SECRET" not in str(tr)


@pytest.mark.asyncio
async def test_ambiguous_ack_is_unknown_and_no_retry(monkeypatch):
    client,public,g,tr,times=await setup(0)
    monkeypatch.setattr(m,"require_certified_transport_permit",lambda p:p)
    client.ack={"id":"NOT_AN_ORDER_ID"}
    with pytest.raises(m.V40PortfolioPostHalted,match="ACK_AMBIGUOUS_RECONCILE_NO_RETRY"):
        await tr.submit_short_with_stop(intent(),permit=permit())
    with pytest.raises(m.V40PortfolioPostHalted,match="ALREADY_POSTED_RECONCILE_NO_RETRY"):
        await tr.submit_short_with_stop(intent(),permit=permit())
    assert len(client.posts)==1


@pytest.mark.asyncio
@pytest.mark.parametrize("change",[
    {"contractSize":"0.01"},{"contractSize":"1.1"},
    {"priceUnit":"0.6"},
])
async def test_actual_contract_size_and_stop_tick_at_POST_must_match_frozen_lot(
    monkeypatch,change,
):
    client,public,g,tr,_=await setup(0)
    public.contract.update(change)
    monkeypatch.setattr(m,"require_certified_transport_permit",lambda p:p)
    with pytest.raises(m.V40PortfolioPostHalted):
        await tr.submit_short_with_stop(intent(),permit=permit())
    assert client.posts==[]


def test_available_margin_uses_documented_balance_not_invented_availableOpen():
    from app.t100_v40_portfolio_order_transport import _asset_usdt
    row=dict(
        currency="USDT",equity="90",cashBalance="100",
        unrealized="-10",availableBalance="60",
        positionMargin="40",debtAmount="0",bonus="0",
    )
    eq,avail=_asset_usdt([row])
    assert eq==D("90")
    assert avail==D("50")  # more conservative than nominal free balance
    row["unrealized"]="10"
    row["equity"]="110"
    eq,avail=_asset_usdt([row])
    assert eq==D("110") and avail==D("60")
    row.pop("unrealized")
    with pytest.raises(m.V40PortfolioPostHalted,match="UNREALIZED_PNL_UNKNOWN"):
        _asset_usdt([row])
