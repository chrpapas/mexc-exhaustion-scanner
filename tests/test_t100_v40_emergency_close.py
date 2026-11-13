"""V40 emergency close exact position and one irrevocable POST (OFFLINE)."""
import asyncio
from dataclasses import replace
from decimal import Decimal as D

import pytest

from app.t100_live_four_slot_snapshot_coordinator import LedgerSnapshot
from app.t100_v40_emergency_close import (
    V40ExitFrame,V40EmergencyCloseHalted,V40EmergencyCloseAdapter,
    attest_missing_native_stop_close,close_once_after_durable_claim,
)

ORIGIN="t100v1e"+"a"*20


def checkpoint():
    return {
        "position_id":"9801","symbol":"DOGE_USDT",
        "external_oid":ORIGIN,"exchange_order_id":"739206374277809664",
        "contracts":"1","entry_price":"100","stop_order_id":"13511",
        "stop_price":"175","best_profit_pct":"0","armed":False,
        "lifecycle_state":"OPEN","checkpoint_version":1,
    }


def ledger():
    cp=checkpoint()
    return LedgerSnapshot(
        checkpoints=[cp],
        revisions=[{
            "position_id":9801,"checkpoint_version":1,
            "external_oid":ORIGIN,"exchange_order_id":cp["exchange_order_id"],
            "lifecycle_state":"OPEN",
        }],
        intents=[{
            "symbol":"DOGE_USDT","external_oid":ORIGIN,
            "exchange_order_id":"739206374277809664",
            "volume_contracts":"1","state":"ACK",
        }],
        stop_amendments=[],readonly_repeatable_read=True,
    )


def position():
    return {
        "positionId":"9801","symbol":"DOGE_USDT","positionType":2,
        "openType":2,"leverage":1,"state":1,
        "holdVol":"1","frozenVol":"0",
    }


def frame(**changes):
    fields=dict(
        positions=[position()],symbol_positions=[position()],
        stops=[],symbol_stops=[],
        counts={
            "sumCount":0,"limitOrderCount":0,"stopOrderCount":0,
            "planOrderCount":0,"trackOrderCount":0,
        },hedge_mode=1,
    )
    fields.update(changes)
    return V40ExitFrame(**fields)


def order():
    return dict(
        symbol="DOGE_USDT",externalOid=ORIGIN,
        orderId="739206374277809664",positionId="9801",
        side=3,state=3,openType=2,leverage=1,dealVol="1",
    )


def target(**override):
    kwargs=dict(
        ledger=ledger(),first=frame(),second=frame(),
        origin_order=order(),reference_ask="101",
        original_notional_usdt="10",
    )
    kwargs.update(override)
    return attest_missing_native_stop_close(**kwargs)


def test_exact_durable_original_and_double_signed_missing_stop_mints_only_target():
    item=target()
    assert item.symbol=="DOGE_USDT" and item.position_id=="9801"
    assert item.quantity_contracts==D("1")
    assert item.close_external_oid.startswith("t100v40x")
    assert item.original_notional_usdt==D("10")
    assert item.entry_allowed is False
    assert item.other_position_adopted is False
    assert item.cause=="NATIVE_STOP_MISSING_DOUBLE_GET"
    assert target().close_external_oid==item.close_external_oid


@pytest.mark.parametrize("override,code",[
    ({"first":frame(stops=[{"id":"99"}])},
     "V40_EMERGENCY_CLOSE_ACCOUNT_NOT_SINGLE_UNPROTECTED_SHORT"),
    ({"second":frame(counts={**frame().counts,"sumCount":1,"stopOrderCount":1})},
     "V40_EMERGENCY_CLOSE_OTHER_OPEN_ORDERS_OR_STOPS"),
    ({"first":frame(positions=[position(),position()])},
     "V40_EMERGENCY_CLOSE_ACCOUNT_NOT_SINGLE_UNPROTECTED_SHORT"),
    ({"first":frame(hedge_mode=2)},
     "V40_EMERGENCY_CLOSE_ACCOUNT_NOT_SINGLE_UNPROTECTED_SHORT"),
    ({"first":frame(positions=[{**position(),"positionType":1}])},
     "V40_EMERGENCY_CLOSE_EXACT_MANAGED_SHORT_DRIFT"),
    ({"first":frame(positions=[{**position(),"frozenVol":"1"}])},
     "V40_EMERGENCY_CLOSE_EXACT_MANAGED_SHORT_DRIFT"),
    ({"second":frame(symbol_positions=[{**position(),"positionId":"9"}])},
     "V40_EMERGENCY_CLOSE_EXACT_MANAGED_SHORT_DRIFT"),
    ({"origin_order":{**order(),"side":2}},
     "V40_EMERGENCY_CLOSE_ORIGIN_FILL_NOT_PROVEN"),
    ({"origin_order":{**order(),"dealVol":"0.5"}},
     "V40_EMERGENCY_CLOSE_ORIGIN_FILL_NOT_PROVEN"),
    ({"original_notional_usdt":"12.51"},
     "V40_EMERGENCY_CLOSE_PILOT_CAP_OR_MARKET_PRICE_INVALID"),
    ({"reference_ask":"NaN"},
     "V40_EMERGENCY_CLOSE_PILOT_CAP_OR_MARKET_PRICE_INVALID"),
    ({"ledger":replace(ledger(),readonly_repeatable_read=False)},
     "V40_EMERGENCY_CLOSE_DURABLE_SINGLE_OWNER_UNVERIFIED"),
    ({"ledger":replace(ledger(),revisions=[])},
     "V40_EMERGENCY_CLOSE_DURABLE_SINGLE_OWNER_UNVERIFIED"),
    ({"ledger":replace(ledger(),intents=[{**ledger().intents[0],"state":"UNKNOWN"}])},
     "V40_EMERGENCY_CLOSE_ENTRY_INTENT_UNRESOLVED"),
    ({"ledger":replace(ledger(),stop_amendments=[{"state":"SUBMITTING"}])},
     "V40_EMERGENCY_CLOSE_DURABLE_SINGLE_OWNER_UNVERIFIED"),
])
def test_missing_stop_is_not_permission_to_close_manual_or_ambiguous_position(override,code):
    with pytest.raises(V40EmergencyCloseHalted,match=code):
        target(**override)


class FakeClaim:
    def __init__(self,allowed=True,fail=False):
        self.allowed=allowed
        self.fail=fail
        self.calls=0
    async def reserve_one(self,t):
        self.calls+=1
        if self.fail:raise RuntimeError("SECRET_DATABASE_URL")
        if self.calls>1:return False
        return self.allowed


class FakePost:
    def __init__(self,reply="739206374277809664",fail=False):
        self.posts=[]
        self.reply=reply
        self.fail=fail
    async def _private_post(self,path,payload):
        self.posts.append((path,payload))
        if self.fail:raise TimeoutError("SECRET_MEXC_KEY")
        return self.reply


def go(claim,post):
    return asyncio.run(close_once_after_durable_claim(
        target=target(),claimant=claim,
        adapter=V40EmergencyCloseAdapter(post),
    ))


def test_once_only_close_short_cross_1x_payload_no_reduce_only():
    claim,post=FakeClaim(),FakePost()
    r=go(claim,post)
    assert r.status=="V40_EMERGENCY_CLOSE_POST_ACK_RECONCILE_ONLY"
    assert r.order_id=="739206374277809664"
    assert r.post_may_have_occurred is True
    assert r.flat_account_verified is False
    assert r.no_second_post_allowed is True
    assert len(post.posts)==1 and claim.calls==1
    path,body=post.posts[0]
    assert path=="/api/v1/private/order/create"
    assert body["side"]==2 and body["type"]==5
    assert body["openType"]==2 and body["positionMode"]==1
    assert body["positionId"]==9801
    assert body["vol"]=="1" and body["price"]=="101"
    assert body["externalOid"]==target().close_external_oid
    assert "reduceOnly" not in body
    assert "stopLossPrice" not in body
    with pytest.raises(V40EmergencyCloseHalted,match="ALREADY_CLAIMED"):
        go(claim,post)
    assert len(post.posts)==1


def test_db_claim_uncertain_or_previously_claimed_cannot_submit_post():
    for claim in (FakeClaim(allowed=False),FakeClaim(fail=True)):
        post=FakePost()
        with pytest.raises(V40EmergencyCloseHalted):
            go(claim,post)
        assert post.posts==[]


def test_after_claim_http_timeout_does_not_retry_or_leak_credential():
    claim,post=FakeClaim(),FakePost(fail=True)
    result=go(claim,post)
    assert result.status=="V40_EMERGENCY_CLOSE_POST_OUTCOME_UNKNOWN_NO_RETRY"
    assert result.order_id is None
    assert len(post.posts)==1
    assert "SECRET" not in repr(result)
    with pytest.raises(V40EmergencyCloseHalted):
        go(claim,post)
    assert len(post.posts)==1


@pytest.mark.parametrize("reply",[
    None,{},{"ts":1},"",False,"NOT_AN_ORDER_ID",
])
def test_malformed_exchange_ack_consumes_claim_no_second_post(reply):
    claim,post=FakeClaim(),FakePost(reply=reply)
    r=go(claim,post)
    assert r.status=="V40_EMERGENCY_CLOSE_POST_OUTCOME_UNKNOWN_NO_RETRY"
    assert r.order_id is None
    assert len(post.posts)==1
    with pytest.raises(V40EmergencyCloseHalted):
        go(claim,post)
    assert len(post.posts)==1
