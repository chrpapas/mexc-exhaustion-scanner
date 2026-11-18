"""Exact 1–4 owned V40 native trailing stop POST and durable no-retry tests.

All exchange and SQL interfaces synthetic. NEVER use real account credentials.
The global production permit and native-stop status remain NOT CERTIFIED.
"""
from __future__ import annotations

from dataclasses import replace
from decimal import Decimal as D
from types import SimpleNamespace
from unittest.mock import patch
import pytest

from app.t100_live_durable_recovery import DurableRecoveryReport
from app.t100_live_four_slot_ledger_reader import PostgresFourSlotLedgerReader
from app.t100_live_four_slot_snapshot_coordinator import (
    FourSlotSnapshotResult,_venue_digest,
)
from app.t100_live_stop_amendment_coordinator import StopAmendmentLifecycleCoordinator
from app.t100_live_stop_mutation_executor import (
    StopMutationExecutor,StopMutationExecutionHalted,
)
from app.t100_live_supervisor import ManagedShort
from app import t100_v40_native_stop_amend_transport as base
from app import t100_v40_portfolio_native_stop_transport as m
from tests.test_t100_v40_native_stop_amend_transport import (
    checkpoint,row_position,row_stop,preflight,
    healthy_guardian,GuardianReader,Client,post_for,cert,MemoryJournal,
)


def checkpoints(n):
    first=checkpoint()
    others=tuple(replace(
        first,symbol=f"ALT{i}_USDT",
        position_id=str(52+i),stop_order_id=str(92+i),
        exchange_order_id=str(91+i),external_oid=f"source{i}",
    ) for i in range(1,n))
    return (first,)+others


def client_for(cps):
    c=Client()
    c.positions=[]
    c.stops=[]
    for cp in cps:
        position=row_position()
        position.update(
            positionId=int(cp.position_id),symbol=cp.symbol,
            holdVol=str(cp.contracts),leverage=3,
        )
        stop=row_stop()
        stop.update(
            id=int(cp.stop_order_id),positionId=int(cp.position_id),
            orderId=int(cp.exchange_order_id),symbol=cp.symbol,
            stopLossVol=str(cp.contracts),
        )
        c.positions.append(position)
        c.stops.append(stop)
    c.counts["sumCount"]=len(cps)
    c.counts["stopOrderCount"]=len(cps)
    c._private_post=post_for(c)
    return c


def snapshot(cps,client,*,ledger_hash="a"*64):
    rows=tuple(ManagedShort(
        symbol=cp.symbol,position_id=cp.position_id,
        contracts=cp.contracts,entry_price=cp.entry_price,
        stop_order_id=cp.stop_order_id,stop_price=cp.stop_price,
        best_profit_pct=cp.best_profit_pct,armed=cp.armed,
    ) for cp in cps)
    return FourSlotSnapshotResult(
        status="NONFLAT_PROVENANCE_MATCHED_NONAUTHORIZING",
        occupied_slots=len(cps),
        recovery=DurableRecoveryReport(
            recovered=rows,checkpoint_count=len(cps),
            exchange_position_count=len(cps),
            exchange_stop_count=len(cps),
        ),
        ledger_hash=ledger_hash,
        critical_inventory_hash=_venue_digest(client.positions,client.stops),
    )


def permit():
    return SimpleNamespace(
        scope="T100_V40_FOUR_SLOT_PRODUCTION_TRIAL",
        symbol="SIREN_USDT",signal_id=497,
    )


async def perform(t,permit_=None):
    return await t.submit_native_short_stop_tightening(
        stop_order_id=92,previous_stop_price="175",
        new_stop_price="99",permit=permit_ or permit(),
    )


def ready(n,monkeypatch):
    cps=checkpoints(n)
    c=client_for(cps)
    ledger=PostgresFourSlotLedgerReader.__new__(
        PostgresFourSlotLedgerReader,
    )
    guardian=GuardianReader(healthy_guardian())
    t=m.V40FourSlotNativeStopAmendTransport(
        client=c,checkpoints=cps,
        target_position_id="52",target_signal_id=497,
        preflight=preflight(),guardian_reader=guardian,ledger=ledger,
    )
    full=snapshot(cps,c)
    async def current(self):
        return full
    monkeypatch.setattr(m.V40FourSlotNativeStopAmendTransport,
                        "_snapshot",current)
    monkeypatch.setattr(base,"run_current_inventory_completeness_probe",cert)
    monkeypatch.setattr(m,"require_certified_transport_permit",lambda p:p)
    return c,t,guardian


@pytest.mark.asyncio
async def test_actual_production_stop_contract_remains_off_before_any_get_or_post(monkeypatch):
    c,t,g=ready(4,monkeypatch)
    with pytest.raises(
        m.V40PortfolioStopPostHalted,match="NOT_CERTIFIED_NO_POST",
    ):
        await perform(t)
    assert c.gets==0 and c.posts==[] and g.calls==0


@pytest.mark.asyncio
@pytest.mark.parametrize("n",[1,2,3,4])
async def test_all_owned_stops_protected_one_exact_trailing_amendment(n,monkeypatch):
    c,t,g=ready(n,monkeypatch)
    with patch("app.t100_live_guards.LIVE_NATIVE_STOP_AMENDMENT_STATUS","CERTIFIED"):
        await perform(t)
        assert c.posts==[(
            "/api/v1/private/stoporder/change_plan_price",
            {"stopPlanOrderId":92,"stopLossPrice":"99",
             "lossTrend":1,"stopLossReverse":2},
        )]
        assert g.calls==2
        assert c.gets>=8
        assert t._post_attempted is True
        with pytest.raises(
            m.V40PortfolioStopPostHalted,
            match="PREVIOUS_POST_MAY_HAVE_OCCURRED_NO_RETRY",
        ):
            await perform(t)
        assert len(c.posts)==1


@pytest.mark.asyncio
async def test_stop_cannot_be_relaxed_or_submitted_by_canary_permit(monkeypatch):
    c,t,g=ready(3,monkeypatch)
    canary=permit()
    canary.scope="T100_V40_SUPERVISED_FIRST_SIGNAL"
    with patch("app.t100_live_guards.LIVE_NATIVE_STOP_AMENDMENT_STATUS","CERTIFIED"):
        with pytest.raises(m.V40PortfolioStopPostHalted,match="IDENTITY_DRIFT"):
            await perform(t,canary)
        with pytest.raises(m.V40PortfolioStopPostHalted,match="IDENTITY_DRIFT"):
            await t.submit_native_short_stop_tightening(
                stop_order_id=92,previous_stop_price="175",
                new_stop_price="176",permit=permit(),
            )
    assert not c.posts and c.gets==0


@pytest.mark.asyncio
@pytest.mark.parametrize("field",[
    "positionId","symbol","stop_price","quantity","stop_id",
])
async def test_non_target_owner_or_native_stop_drift_blocks_post(monkeypatch,field):
    c,t,g=ready(4,monkeypatch)
    if field=="positionId":
        c.stops[3]["positionId"]=999
    elif field=="symbol":
        c.positions[3]["symbol"]="UNOWNED_USDT"
    elif field=="stop_price":
        c.stops[3]["stopLossPrice"]="170"
    elif field=="quantity":
        c.stops[3]["stopLossVol"]="0.5"
    elif field=="stop_id":
        c.stops[3]["id"]=999
    with patch("app.t100_live_guards.LIVE_NATIVE_STOP_AMENDMENT_STATUS","CERTIFIED"):
        with pytest.raises(
            m.V40PortfolioStopPostHalted,match="(ACCOUNT_LEDGER|BOOKEND_DRIFT)",
        ):
            await perform(t)
    assert c.posts==[]


@pytest.mark.asyncio
async def test_guardian_latch_second_read_blocks_post(monkeypatch):
    c,t,g=ready(2,monkeypatch)
    async def guarded():
        g.calls+=1
        if g.calls>1:
            return replace(healthy_guardian(),
                no_current_scope_critical_or_pending_incidents=False)
        return healthy_guardian()
    g.verify=guarded
    with patch("app.t100_live_guards.LIVE_NATIVE_STOP_AMENDMENT_STATUS","CERTIFIED"):
        with pytest.raises(m.V40PortfolioStopPostHalted,match="GUARDIAN_FAILED"):
            await perform(t)
    assert c.posts==[] and g.calls==2


@pytest.mark.asyncio
async def test_ledger_or_venue_hash_change_is_unknown_and_never_posts(monkeypatch):
    c,t,g=ready(2,monkeypatch)
    count=0
    original=t._snapshot
    async def changing():
        nonlocal count
        count+=1
        snap=await original()
        return snap if count==1 else replace(snap,ledger_hash="b"*64)
    t._snapshot=changing
    with patch("app.t100_live_guards.LIVE_NATIVE_STOP_AMENDMENT_STATUS","CERTIFIED"):
        with pytest.raises(m.V40PortfolioStopPostHalted,match="BOOKEND_DRIFT"):
            await perform(t)
    assert len(c.posts)==0 and count==2


@pytest.mark.asyncio
async def test_exchange_response_unknown_no_retry(monkeypatch):
    c,t,g=ready(4,monkeypatch)
    async def timeout(path,body):
        c.posts.append((path,body))
        raise TimeoutError("PRIVATE_AUTHENTICATED_URL")
    c._private_post=timeout
    with patch("app.t100_live_guards.LIVE_NATIVE_STOP_AMENDMENT_STATUS","CERTIFIED"):
        with pytest.raises(m.V40PortfolioStopPostHalted,match="OUTCOME_UNKNOWN"):
            await perform(t)
        with pytest.raises(m.V40PortfolioStopPostHalted,match="NO_RETRY"):
            await perform(t)
    assert len(c.posts)==1


@pytest.mark.asyncio
async def test_durable_stop_submitting_before_portfolio_post(monkeypatch):
    c,t,g=ready(4,monkeypatch)
    journal=MemoryJournal()
    lifecycle=StopAmendmentLifecycleCoordinator(journal)
    amendment,state=await lifecycle.prepare(preflight().proposal)
    assert state=="PREPARED"
    executor=StopMutationExecutor(lifecycle=lifecycle,transport=t)
    with patch("app.t100_live_guards.LIVE_NATIVE_STOP_AMENDMENT_STATUS","CERTIFIED"),patch(
        "app.t100_live_stop_mutation_executor.require_certified_transport_permit",
        lambda x:x,
    ):
        result=await executor.submit_once(amendment,permit=permit())
        assert result.journal_state=="SUBMITTING"
        assert journal.rows[amendment.external_id]=="SUBMITTING"
        assert len(c.posts)==1
        with pytest.raises(StopMutationExecutionHalted,match="RECONCILE_ONLY"):
            await executor.submit_once(amendment,permit=permit())
    assert len(c.posts)==1


@pytest.mark.asyncio
async def test_ambiguous_stop_post_marks_durable_unknown(monkeypatch):
    c,t,g=ready(3,monkeypatch)
    async def timeout(path,body):
        c.posts.append((path,body))
        raise TimeoutError("PRIVATE_AUTHENTICATED_URL")
    c._private_post=timeout
    journal=MemoryJournal()
    lifecycle=StopAmendmentLifecycleCoordinator(journal)
    amendment,_=await lifecycle.prepare(preflight().proposal)
    executor=StopMutationExecutor(lifecycle=lifecycle,transport=t)
    with patch("app.t100_live_guards.LIVE_NATIVE_STOP_AMENDMENT_STATUS","CERTIFIED"),patch(
        "app.t100_live_stop_mutation_executor.require_certified_transport_permit",
        lambda x:x,
    ):
        with pytest.raises(StopMutationExecutionHalted,match="RECONCILE_ONLY"):
            await executor.submit_once(amendment,permit=permit())
        assert journal.rows[amendment.external_id]=="UNKNOWN"
        with pytest.raises(StopMutationExecutionHalted,match="RECONCILE_ONLY"):
            await executor.submit_once(amendment,permit=permit())
    assert len(c.posts)==1


def test_constructor_rejects_duplicate_portfolio_or_wrong_ledger():
    from app.t100_v40_portfolio_native_stop_transport import (
        V40FourSlotNativeStopAmendTransport,V40PortfolioStopPostHalted,
    )
    cp=checkpoint()
    c=Client()
    c._private_post=post_for(c)
    ledger=PostgresFourSlotLedgerReader.__new__(
        PostgresFourSlotLedgerReader
    )
    kwargs=dict(
        client=c,preflight=preflight(),target_signal_id=497,
        target_position_id=cp.position_id,
        guardian_reader=GuardianReader(healthy_guardian()),
        ledger=ledger,
    )
    with pytest.raises(V40PortfolioStopPostHalted,match="DUPLICATE_POSITION"):
        V40FourSlotNativeStopAmendTransport(
            checkpoints=(cp,cp),**kwargs,
        )
    with pytest.raises(V40PortfolioStopPostHalted,match="LEDGER_REQUIRED"):
        V40FourSlotNativeStopAmendTransport(
            checkpoints=(cp,),**{**kwargs,"ledger":object()},
        )
