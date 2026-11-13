"""Four-slot V40 guardian read-only invariant tests, no account mutation."""
import asyncio
from copy import deepcopy

import pytest

from app.t100_v40_four_slot_guardian_policy import audit_frozen_four_slot_native_stops
from app.t100_live_four_slot_snapshot_coordinator import FourSlotSnapshotCoordinator
from tests.test_t100_live_four_slot_snapshot_coordinator import (
    fixture,report,Ledger,Venue,Completeness,
)


def evidence(n:int):
    ld,orders,positions,stops=fixture(n)
    for i,(pos,stop) in enumerate(zip(positions,stops)):
        pos.update({"openType":2,"leverage":5})
        stop.update({
            "isFinished":0,"positionType":2,"orderId":str(100+i),
            "triggerSide":0,"errorCode":0,"stopLossType":0,
            "stopLossReverse":2,"lossTrend":1,
            "profitLossVolType":"SAME","volType":1,
        })
    ven=Venue(orders,positions,stops)
    snap=asyncio.run(FourSlotSnapshotCoordinator(
        ledger=Ledger(ld),venue=ven,completeness=Completeness(report(n))
    ).inspect())
    return snap,ld,ven.last_positions,ven.last_stops


@pytest.mark.parametrize("n",[1,2,3,4])
def test_one_to_four_independently_owned_protected_cross_shorts(n):
    snap,ld,positions,stops=evidence(n)
    assert snap.status=="NONFLAT_PROVENANCE_MATCHED_NONAUTHORIZING"
    out=audit_frozen_four_slot_native_stops(
        snapshot=snap,ledger=ld,account_positions=positions,
        account_stops=stops,
    )
    assert out.status=="V40_FOUR_SLOT_ACCOUNT_NATIVE_MARKET_SL_SIGNED_GET_NONAUTHORIZING"
    assert out.protected_owned_positions==n
    assert out.matching_native_market_stops==n
    assert out.separate_symbols_verified
    assert out.account_bookends_verified
    assert out.ledger_bookends_verified
    assert out.all_owned_cross_positions_verified
    assert not out.incident_alert
    assert not out.exchange_margin_and_liquidation_certified
    assert not out.native_stop_execution_certified
    assert not out.entry_allowed and not out.live_authorization
    assert out.mexc_posts==out.database_writes==0


@pytest.mark.parametrize("change",[
    ("positions",0,"openType",1),
    ("positions",0,"positionType",1),
    ("positions",0,"leverage",0),
    ("positions",0,"state",3),
    ("positions",0,"holdVol","999"),
    ("stops",0,"stopLossType",1),
    ("stops",0,"triggerSide",2),
    ("stops",0,"stopLossPrice","176"),
    ("stops",0,"errorCode",5005),
    ("stops",0,"orderId","999"),
    ("stops",0,"id","555"),
])
def test_any_margin_side_quantity_origin_or_stop_error_halts_entire_account(change):
    snap,ld,positions,stops=evidence(4)
    target,i,key,value=change
    if target=="positions":positions=deepcopy(positions);positions[i][key]=value
    else:stops=deepcopy(stops);stops[i][key]=value
    out=audit_frozen_four_slot_native_stops(
        snapshot=snap,ledger=ld,account_positions=positions,
        account_stops=stops,
    )
    assert out.incident_alert
    assert out.live_authorization is False
    assert out.protected_owned_positions==0


def test_fifth_unowned_position_is_not_adopted():
    snap,ld,positions,stops=evidence(4)
    positions=deepcopy(positions)+[dict(positions[0],positionId="999")]
    out=audit_frozen_four_slot_native_stops(
        snapshot=snap,ledger=ld,
        account_positions=positions,account_stops=stops,
    )
    assert out.status=="V40_FOUR_SLOT_GUARDIAN_VENUE_BOOKEND_DRIFT"


def test_ledger_drift_is_not_treated_as_venue_stop_verification():
    snap,ld,positions,stops=evidence(3)
    ld.checkpoints[0]["stop_price"]="999"
    out=audit_frozen_four_slot_native_stops(
        snapshot=snap,ledger=ld,
        account_positions=positions,account_stops=stops,
    )
    assert out.status=="V40_FOUR_SLOT_GUARDIAN_LEDGER_DRIFT"


def test_no_nonflat_position_is_guardian_stop_certification():
    snap,ld,positions,stops=evidence(1)
    out=audit_frozen_four_slot_native_stops(
        snapshot=snap,ledger=ld,account_positions=[],
        account_stops=[],
    )
    assert out.incident_alert
    assert out.protected_owned_positions==0
