"""Offline-only V40 CROSS guardian, fail-closed and no MEXC mutations."""
from __future__ import annotations

import asyncio
from copy import deepcopy
from dataclasses import replace
from decimal import Decimal as D
import hashlib

import pytest

from app.t100_live_readonly_probe import ReadOnlyMexcProbe
from app.t100_v40_cross_watchdog_observer import (
    V40CrossWatchScope,assess_v40_cross_watch,
    observe_v40_cross_get,validate_v40_scope,
)

HASH=hashlib.sha256(b"DEDICATED_READONLY_KEY").hexdigest()
OID="t100v40pilot_entry_12345"


def flat_scope():
    return V40CrossWatchScope(account_key_sha256=HASH,mode="FLAT")


def owned_scope():
    return V40CrossWatchScope(
        account_key_sha256=HASH,mode="OWNED",symbol="EXAMPLE_USDT",
        position_id="900",origin_oid=OID,entry_order_id="123456",
        stop_id="99",contracts=D("250"),max_short_stop_price=D("3.5"),
    )


def inventory(*,owned=False):
    position={
        "symbol":"EXAMPLE_USDT","positionId":"900","state":1,
        "positionType":2,"openType":2,"leverage":1,
        "holdVol":"250",
    }
    stop={
        "symbol":"EXAMPLE_USDT","id":"99","positionId":"900",
        "state":1,"isFinished":0,"positionType":2,"orderId":"123456",
        "triggerSide":0,"errorCode":0,"vol":"0",
        "profitLossVolType":"SAME","volType":2,
        "stopLossType":0,"stopLossReverse":2,"lossTrend":1,
        "stopLossPrice":"3.5",
    }
    count=1 if owned else 0
    return {
        "positions":[position] if owned else [],
        "stops":[stop] if owned else [],
        "counts":{
            "sumCount":count,"stopOrderCount":count,
            "limitOrderCount":0,"planOrderCount":0,"trackOrderCount":0,
        },
        "mode":1,
    }


def order():
    return {
        "symbol":"EXAMPLE_USDT","externalOid":OID,
        "orderId":"123456","positionId":"900",
        "state":3,"side":3,"dealVol":"250",
    }


def deals():
    return [
        {"id":"1","orderId":"123456","symbol":"EXAMPLE_USDT",
         "side":3,"price":"3.2","vol":"100"},
        {"id":"2","orderId":"123456","symbol":"EXAMPLE_USDT",
         "side":3,"price":"3.3","vol":"150"},
    ]


def assess(scope,first=None,second=None,**kw):
    baseline=inventory(owned=scope.mode=="OWNED")
    return assess_v40_cross_watch(
        scope,baseline if first is None else first,
        deepcopy(baseline) if second is None else second,
        order=order() if scope.mode=="OWNED" else None,
        deals=deals() if scope.mode=="OWNED" else None,
        **kw,
    )


def test_flat_scope_normal_double_read_is_non_authorizing():
    result=assess(flat_scope())
    assert result.status=="WATCH_FLAT_INVENTORY_OBSERVED_NONAUTHORIZING"
    assert result.account_flat_observed
    assert not result.alert_supervisor
    assert not result.currently_active_stop_observed
    assert result.read_only
    assert not result.live_authorization
    assert not result.entry_allowed
    assert not result.continuous_protection_certified


def test_cross_1x_pinned_owned_native_market_stop_is_observation_only():
    result=assess(owned_scope())
    assert result.status=="WATCH_NATIVE_STOP_CURRENTLY_OBSERVED_NONAUTHORIZING"
    assert result.currently_active_stop_observed
    assert not result.alert_supervisor
    assert not result.stop_execution_certified
    assert not result.continuous_protection_certified
    assert not result.live_authorization
    assert not result.entry_allowed
    assert not result.mexc_orders_sent


def test_flat_does_not_silently_adopt_a_new_position():
    result=assess_v40_cross_watch(
        flat_scope(),inventory(),inventory(owned=True),
    )
    assert result.alert_supervisor
    assert not result.account_flat_observed
    result=assess_v40_cross_watch(
        flat_scope(),inventory(owned=True),inventory(owned=True),
    )
    assert result.status=="WATCH_V40_FLAT_SCOPE_UNEXPECTED_EXPOSURE"


@pytest.mark.parametrize("field,value",[
    ("openType",1),("leverage",2),("holdVol","249"),
    ("positionType",1),("state",2),("positionId","901"),
    ("symbol","OTHER_USDT"),
])
def test_owned_wrong_margin_quantity_identity_or_state_blocks(field,value):
    bad=inventory(owned=True)
    bad["positions"][0][field]=value
    result=assess_v40_cross_watch(
        owned_scope(),bad,deepcopy(bad),order=order(),deals=deals(),
    )
    assert result.status=="WATCH_V40_OWNED_CROSS_POSITION_UNVERIFIED"
    assert result.alert_supervisor


@pytest.mark.parametrize("field,value",[
    ("stopLossPrice","3.6"),("stopLossType",1),
    ("stopLossType",None),("triggerSide",1),("errorCode",1),
    ("volType",3),("stopLossReverse",1),("lossTrend",2),
    ("isFinished",1),("positionType",1),("vol","3"),
])
def test_native_stop_missing_or_wrong_semantics_blocks(field,value):
    bad=inventory(owned=True)
    bad["stops"][0][field]=value
    result=assess_v40_cross_watch(
        owned_scope(),bad,deepcopy(bad),order=order(),deals=deals(),
    )
    assert result.status=="WATCH_V40_NATIVE_MARKET_STOP_NOT_VERIFIED"
    assert result.alert_supervisor


def test_stop_replacement_cannot_be_adopted_automatically():
    bad=inventory(owned=True)
    bad["stops"][0]["id"]="100"
    result=assess_v40_cross_watch(
        owned_scope(),bad,deepcopy(bad),order=order(),deals=deals(),
    )
    assert result.status=="WATCH_V40_NATIVE_STOP_IDENTITY_CHANGED"
    assert result.alert_supervisor


@pytest.mark.parametrize("change",[
    "extra_position","extra_stop","other_pending","missing_stop",
    "stop_count_mismatch","bad_hedge_mode",
])
def test_unowned_manual_or_ambiguous_inventory_is_alerted(change):
    bad=inventory(owned=True)
    if change=="extra_position":
        r=deepcopy(bad["positions"][0])
        r["positionId"]="901"
        bad["positions"].append(r)
    elif change=="extra_stop":
        r=deepcopy(bad["stops"][0])
        r["id"]="100"
        bad["stops"].append(r)
        bad["counts"]["sumCount"]=2
        bad["counts"]["stopOrderCount"]=2
    elif change=="other_pending":
        bad["counts"]["sumCount"]=2
        bad["counts"]["limitOrderCount"]=1
    elif change=="missing_stop":
        bad["stops"].clear()
        bad["counts"]["sumCount"]=0
        bad["counts"]["stopOrderCount"]=0
    elif change=="stop_count_mismatch":
        bad["counts"]["stopOrderCount"]=0
    else:
        bad["mode"]=2
    result=assess_v40_cross_watch(
        owned_scope(),bad,deepcopy(bad),order=order(),deals=deals(),
    )
    assert result.alert_supervisor
    assert not result.currently_active_stop_observed
    assert result.entry_allowed is False


@pytest.mark.parametrize("field,value",[
    ("externalOid","WRONG"),("state",2),("orderId","999"),
    ("positionId","901"),("side",4),("dealVol","249"),
])
def test_entry_origin_identity_must_match_and_be_filled(field,value):
    changed=order()
    changed[field]=value
    result=assess_v40_cross_watch(
        owned_scope(),inventory(owned=True),inventory(owned=True),
        order=changed,deals=deals(),
    )
    assert result.status=="WATCH_V40_DURABLE_ORIGIN_AND_FILLS_UNVERIFIED"


@pytest.mark.parametrize("change",["duplicate","wrong_order","wrong_side","short_quantity"])
def test_entry_deal_ledger_cannot_be_partial_or_duplicated(change):
    d=deals()
    if change=="duplicate":d[1]["id"]="1"
    if change=="wrong_order":d[1]["orderId"]="8"
    if change=="wrong_side":d[1]["side"]=4
    if change=="short_quantity":d[1]["vol"]="149"
    result=assess_v40_cross_watch(
        owned_scope(),inventory(owned=True),inventory(owned=True),
        order=order(),deals=d,
    )
    assert result.status=="WATCH_V40_DURABLE_ORIGIN_AND_FILLS_UNVERIFIED"


def test_position_or_stop_can_change_between_bookends_and_must_alert():
    a=inventory(owned=True)
    b=deepcopy(a)
    b["stops"][0]["stopLossPrice"]="3.4"
    result=assess_v40_cross_watch(
        owned_scope(),a,b,order=order(),deals=deals(),
    )
    assert result.status=="WATCH_V40_CRITICAL_BOOKEND_DRIFT"


@pytest.mark.parametrize("bad",[
    replace(owned_scope(),mode="FLAT"),
    replace(owned_scope(),symbol="BTC_USDT"),
    replace(owned_scope(),position_id="0"),
    replace(owned_scope(),entry_order_id="bad"),
    replace(owned_scope(),stop_id=None),
    replace(owned_scope(),contracts=D("NaN")),
    replace(owned_scope(),max_short_stop_price=D("0")),
    replace(owned_scope(),account_key_sha256="wrong"),
])
def test_bad_scope_cannot_start_owned_watcher(bad):
    assert not validate_v40_scope(bad)
    assert assess_v40_cross_watch(bad,inventory(),inventory()).alert_supervisor


class FakeTransport:
    def __init__(self,with_owned=False,fail=False):
        data=inventory(owned=with_owned)
        self.fail=fail
        self.calls=[]
        self.payloads={
            "/api/v1/private/position/open_positions":data["positions"],
            "/api/v1/private/stoporder/open_orders":data["stops"],
            "/api/v1/private/order/open_order_total_count":data["counts"],
            "/api/v1/private/position/position_mode":{"positionMode":1},
            "/api/v1/private/order/external/EXAMPLE_USDT/"+OID:order(),
            "/api/v1/private/order/deal_details/123456":deals(),
        }
    async def _private_get(self,path,params=None):
        self.calls.append((path,params))
        if self.fail:raise TimeoutError("SECRET_IN_HTTP_EXCEPTION")
        assert params is None
        return deepcopy(self.payloads[path])


def test_signed_get_adapter_only_uses_allowlisted_read_routes():
    transport=FakeTransport(with_owned=True)
    result=asyncio.run(observe_v40_cross_get(
        ReadOnlyMexcProbe(transport),owned_scope(),
    ))
    assert result.currently_active_stop_observed
    assert len(transport.calls)==10
    assert all(path.startswith("/api/v1/private/") for path,_ in transport.calls)
    assert all(params is None for _,params in transport.calls)


def test_signed_get_unavailable_is_not_assumed_flat():
    transport=FakeTransport(fail=True)
    result=asyncio.run(observe_v40_cross_get(
        ReadOnlyMexcProbe(transport),flat_scope(),
    ))
    assert result.status=="WATCH_V40_SIGNED_GET_UNAVAILABLE"
    assert result.alert_supervisor
    assert not result.account_flat_observed
    assert "SECRET" not in str(result)
