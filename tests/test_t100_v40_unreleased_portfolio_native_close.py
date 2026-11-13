"""Four-slot V40 close: exact positive exit, other three remain protected."""
import asyncio
from dataclasses import replace
from decimal import Decimal as D
from types import SimpleNamespace

import pytest

import app.t100_v40_unreleased_portfolio_native_close as m
from app.t100_live_durable_recovery import checkpoint_from_row
from app.t100_live_four_slot_snapshot_coordinator import _venue_digest
from app.t100_live_native_stop_close_recovery import NativeStopRecoveryReport
from app.t100_live_owned_emergency_guardian import OwnedShort
from app.t100_live_readonly_probe import ReadOnlyMexcProbe
from tests.test_t100_live_four_slot_snapshot_coordinator import fixture,report


def closing():
    return OwnedShort(
        scope_sha256="a"*64,symbol="CLOSED_USDT",
        position_id="999",entry_external_oid="t100entrysource001",
        entry_order_id="1009",contracts=D("7"),
        stop_order_id="1099",stop_price=D("175"),
        checkpoint_version=1,
    )


def survivor_rows(n=3):
    ledger,_,positions,stops=fixture(n)
    for i,(pos,stop) in enumerate(zip(positions,stops)):
        pos.update({"openType":2,"leverage":5})
        stop.update({
            "isFinished":0,"positionType":2,"orderId":str(100+i),
            "triggerSide":0,"errorCode":0,"stopLossType":0,
            "stopLossReverse":2,"lossTrend":1,
            "profitLossVolType":"SAME","volType":1,
        })
    cps=tuple(checkpoint_from_row(row) for row in ledger.checkpoints)
    counts=dict(
        sumCount=n,stopOrderCount=n,limitOrderCount=0,
        planOrderCount=0,trackOrderCount=0,
    )
    cert=report(n)
    cert.update(
        critical_inventory_sha256=_venue_digest(positions,stops),
        exchange_atomicity_verified=False,
        live_authorization=False,
        entry_allowed=False,
    )
    return positions,stops,cps,counts,cert


@pytest.mark.parametrize("n",[0,1,2,3])
def test_closing_one_with_no_to_three_remaining_protected_slots(n):
    ps,ss,cps,counts,cert=survivor_rows(n)
    frame=m._verified_survivor_snapshot(
        positions=ps,stops=ss,order_counts=counts,hedge_mode=1,
        certificate=cert,surviving=cps,finished=closing(),
    )
    assert frame.account_digest==_venue_digest(ps,ss)
    assert frame.order_counts==(n,n,0,0,0)


@pytest.mark.parametrize("which,value,reason",[
    ("hedge",0,"SHAPE_INVALID"),
    ("certificate",None,"SHAPE_INVALID"),
    ("cert_shape",False,"COMPLETENESS_UNVERIFIED"),
    ("cert_hash","bogus","COMPLETENESS_UNVERIFIED"),
    ("extra_position",True,"UNRELATED_EXPOSURE_OR_ORDERS"),
    ("no_stops",True,"UNRELATED_EXPOSURE_OR_ORDERS"),
    ("limit_order",1,"UNRELATED_EXPOSURE_OR_ORDERS"),
    ("missing_market_stop",1,"SURVIVING_SHORT_UNPROTECTED"),
    ("wrong_leverage",0,"SURVIVING_SHORT_UNPROTECTED"),
])
def test_any_uncertainty_rejects_per_position_closure(which,value,reason):
    ps,ss,cps,counts,cert=survivor_rows(3)
    kw=dict(
        positions=ps,stops=ss,order_counts=counts,
        hedge_mode=1,certificate=cert,surviving=cps,
        finished=closing(),
    )
    if which=="hedge":kw["hedge_mode"]=value
    if which=="certificate":kw["certificate"]=value
    if which=="cert_shape":cert["account_wide_completeness_verified"]=value
    if which=="cert_hash":cert["critical_inventory_sha256"]=value
    if which=="extra_position":ps.append(dict(ps[0],positionId="888"))
    if which=="no_stops":ss.pop()
    if which=="limit_order":counts.update(sumCount=4,limitOrderCount=1)
    if which=="missing_market_stop":
        ss[0]["stopLossType"]=value
        cert["critical_inventory_sha256"]=_venue_digest(ps,ss)
    if which=="wrong_leverage":ps[0]["leverage"]=value
    with pytest.raises(m.V40PortfolioCloseHalted,match=reason):
        m._verified_survivor_snapshot(**kw)


def instrument(monkeypatch,*,label=1,drift=False,closed=True):
    ps,ss,cps,counts,cert=survivor_rows(3)
    start=m._verified_survivor_snapshot(
        positions=ps,stops=ss,order_counts=counts,hedge_mode=1,
        certificate=cert,surviving=cps,finished=closing(),
    )
    times=[]
    read=0
    async def fake_frame(probe,*,surviving,finished):
        nonlocal read
        read+=1
        if drift and read>=3:
            return replace(start,account_digest="d"*64)
        return start
    async def delay(seconds):
        times.append(seconds)
    async def recovered(**kwargs):
        return NativeStopRecoveryReport(
            status="NATIVE_STOP_CLOSE_AND_OWNED_POSITION_VERIFIED_VENUE_TP_LABEL_CONFLICT",
            entry_filled_verified=closed,
            native_stop_terminal_verified=closed,
            linked_venue_close_filled=closed,
            owned_position_closed_verified=closed,
            venue_trigger_side_conflict=(label==1),
            stop_loss_trigger_side_proven=False,
        )
    async def finder(*args,**kwargs):
        return dict(
            id="1099",positionId="999",symbol="CLOSED_USDT",
            placeOrderId="10091",triggerSide=label,
        )
    def fill(*,order_id,order,fills,require_strategy_fully_filled):
        assert order_id=="10091"
        assert require_strategy_fully_filled
        return SimpleNamespace(
            final_order_fill_history_complete=True,
            strategy_fully_filled_order_complete=True,
            side="CLOSE_SHORT",position_id="999",
            symbol="CLOSED_USDT",deal_volume=D("7"),
        )
    async def read_by_id(self,order_id):
        return dict(orderId=order_id)
    async def read_deals(self,order_id):
        return [dict(tradeId="1")]
    monkeypatch.setattr(m,"_certified_frame",fake_frame)
    monkeypatch.setattr(m,"recover_native_stop_close_read_only",recovered)
    monkeypatch.setattr(m,"find_finished_stop_by_id",finder)
    monkeypatch.setattr(m,"build_verified_order_fill_set",fill)
    monkeypatch.setattr(ReadOnlyMexcProbe,"raw_order_by_id",read_by_id)
    monkeypatch.setattr(ReadOnlyMexcProbe,"raw_order_deal_details",read_deals)
    class OfflinePrivateGet:
        async def _private_get(self,path,params=None):
            raise AssertionError("No other GET needed in fixture")
    return ReadOnlyMexcProbe(OfflinePrivateGet()),cps,delay,times


@pytest.mark.parametrize("side",[1,2])
def test_positive_one_native_close_preserves_other_three_and_does_not_release_slot(monkeypatch,side):
    probe,cps,delay,times=instrument(monkeypatch,label=side)
    r=asyncio.run(m.observe_one_native_exit_among_v40_survivors(
        probe=probe,closing=closing(),
        account_scope_sha256="a"*64,surviving=cps,wait=delay,
    ))
    assert r.status==(
        "V40_PORTFOLIO_CLOSE_EXCHANGE_TP_LABEL_CONFLICT_UNRELEASED"
        if side==1 else "V40_PORTFOLIO_CLOSE_EXCHANGE_SL_LABEL_OBSERVED_UNRELEASED"
    )
    assert r.closed_position_id=="999"
    assert r.surviving_protected_slots==3
    assert r.native_trigger_label_conflict is (side==1)
    assert r.original_native_stop_fill_label_proven is False
    assert r.exact_closed_position_history_verified is True
    assert r.portfolio_unchanged_across_bookends is True
    assert r.slot_released is False
    assert r.live_authorization is False
    assert r.mexc_posts==r.database_writes==0
    assert times==[.3,.3]


def test_missing_positive_history_cannot_free_slot(monkeypatch):
    probe,cps,delay,_=instrument(monkeypatch,closed=False)
    r=asyncio.run(m.observe_one_native_exit_among_v40_survivors(
        probe=probe,closing=closing(),
        account_scope_sha256="a"*64,surviving=cps,wait=delay,
    ))
    assert r.status=="V40_PORTFOLIO_CLOSE_TERMINAL_OR_HISTORY_NOT_PROVEN"
    assert not r.slot_released
    assert r.exact_closed_position_history_verified is False


def test_other_positions_drift_during_stop_history_halts(monkeypatch):
    probe,cps,delay,_=instrument(monkeypatch,drift=True)
    r=asyncio.run(m.observe_one_native_exit_among_v40_survivors(
        probe=probe,closing=closing(),
        account_scope_sha256="a"*64,surviving=cps,wait=delay,
    ))
    assert r.status=="V40_PORTFOLIO_CLOSE_SURVIVORS_CHANGED_DURING_HISTORY"
    assert not r.slot_released
    assert not r.live_authorization
