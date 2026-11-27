"""Verified V40 postfill CROSS 1x/native MARKET stop guardian, offline."""
from __future__ import annotations

import asyncio
from copy import deepcopy

import pytest

from app.t100_live_four_slot_snapshot_coordinator import (
    FourSlotSnapshotCoordinator,
)
from app.t100_v40_guardian_owned_cross import assess_v40_owned_cross_guardian
from tests.test_t100_live_four_slot_snapshot_coordinator import (
    fixture,report,Ledger,Venue,Completeness,
)


def bookend(**changes):
    ledger,orders,positions,stops=fixture(1)
    pos=positions[0]
    pos.update({
        "openType":2,"leverage":1,
    })
    native=stops[0]
    native.update({
        "isFinished":0,"positionType":2,"orderId":"100",
        "triggerSide":0,"errorCode":0,
        "stopLossType":0,"stopLossReverse":2,
        "lossTrend":1,"profitLossVolType":"SAME",
        "volType":1,
    })
    for k,v in changes.items():
        if k=="stop":native.update(v)
        elif k=="position":pos.update(v)
        elif k=="ledger":ledger= v
        else:raise AssertionError(k)
    ld=Ledger(ledger)
    ve=Venue(orders,positions,stops)
    completeness=Completeness(report(1))
    result=asyncio.run(FourSlotSnapshotCoordinator(
        ledger=ld,venue=ve,completeness=completeness,
    ).inspect())
    return result,ledger,ve.last_positions,ve.last_stops


def test_one_durable_cross_short_and_exact_filled_entry_attached_market_stop():
    snap,ledger,positions,stops=bookend()
    assert snap.status=="NONFLAT_PROVENANCE_MATCHED_NONAUTHORIZING"
    out=assess_v40_owned_cross_guardian(
        snapshot=snap,ledger=ledger,
        positions=positions,stops=stops,
    )
    assert out.status=="V40_ONE_OWNED_CROSS_SHORT_NATIVE_MARKET_SL_OBSERVED_NONAUTHORIZING"
    assert out.single_durable_owned_short
    assert out.cross_1x_position_verified
    assert out.native_market_sl_exact_order_and_quantity_verified
    assert out.independent_venue_bookends_observed
    assert out.account_wide_completeness_observed
    assert out.active_positions==out.active_stops==1
    assert not out.incident_alert
    assert not out.live_stop_execution_certified
    assert not out.trailing_execution_certified
    assert not out.guardian_deployment_certified
    assert not out.entry_allowed and not out.live_authorization
    assert out.mexc_posts==0 and out.database_writes==0


@pytest.mark.parametrize("bad",[
    {"position":{"openType":1}},
    {"position":{"leverage":2}},
    {"position":{"positionType":1}},
    {"position":{"holdVol":"9"}},
    {"stop":{"orderId":"101"}},
    {"stop":{"stopLossType":1}},
    {"stop":{"isFinished":1}},
    {"stop":{"triggerSide":2}},
    {"stop":{"stopLossVol":"1","profitLossVolType":"SEPARATE"}},
    {"stop":{"stopLossPrice":"176"}},
    {"stop":{"errorCode":2030}},
])
def test_cross_margin_side_stop_volume_and_origin_are_strict(bad):
    snap,ledger,positions,stops=bookend(**bad)
    out=assess_v40_owned_cross_guardian(
        snapshot=snap,ledger=ledger,
        positions=positions,stops=stops,
    )
    assert out.incident_alert is True
    assert out.entry_allowed is False
    assert out.status.endswith("_HALT")
    assert out.mexc_posts==0


def test_checkpoint_provenance_changed_after_signed_get_fails_closed():
    snap,ledger,positions,stops=bookend()
    ledger.checkpoints[0]["stop_order_id"]="other"
    out=assess_v40_owned_cross_guardian(
        snapshot=snap,ledger=ledger,
        positions=positions,stops=stops,
    )
    assert out.status=="V40_OWNED_CROSS_LEDGER_SNAPSHOT_CHANGED_HALT"


def test_foreign_position_is_not_promoted_to_owned():
    snap,ledger,positions,stops=bookend()
    positions=deepcopy(positions)
    positions.append(deepcopy(positions[0]))
    positions[-1]["positionId"]="999"
    out=assess_v40_owned_cross_guardian(
        snapshot=snap,ledger=ledger,
        positions=positions,stops=stops,
    )
    assert out.incident_alert and not out.single_durable_owned_short
    assert not out.live_authorization
