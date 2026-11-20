"""End-to-end offline source wiring for dedicated V40 0–4 slot guardian."""
import asyncio
from copy import deepcopy

import pytest

from app.t100_v40_four_slot_signed_observer import (
    _observe_verified_four_slot_sources,
    observe_dedicated_v40_four_slot_get_only,
)
from tests.test_t100_live_four_slot_snapshot_coordinator import (
    fixture, report, Ledger, Venue, Completeness,
)


def inputs(n):
    ledger,orders,positions,stops=fixture(n)
    for i,(pos,stop) in enumerate(zip(positions,stops)):
        pos.update({"openType":2,"leverage":5})
        stop.update({
            "isFinished":0,"positionType":2,"orderId":str(100+i),
            "triggerSide":0,"errorCode":0,"stopLossType":0,
            "stopLossReverse":2,"lossTrend":1,
            "profitLossVolType":"SAME","volType":1,
        })
    return Ledger(ledger),Venue(orders,positions,stops),Completeness(report(n))


def watch(n, *,hedge_mode=1):
    ledger,venue,completeness=inputs(n)
    result=asyncio.run(_observe_verified_four_slot_sources(
        ledger=ledger,venue=venue,completeness=completeness,
        hedge_mode=hedge_mode,
    ))
    return result,ledger,venue,completeness


@pytest.mark.parametrize("n",[0,1,2,3,4])
def test_exact_significant_exposure_protection_with_no_live_permit(n):
    r,ledger,venue,complete=watch(n)
    assert r.status==(
        "V40_FOUR_SLOT_SIGNED_ACCOUNT_FLAT_AND_LEDGER_CLEAN"
        if n==0
        else "V40_FOUR_SLOT_SIGNED_ONE_TO_FOUR_NATIVE_SL_PROTECTED_NONAUTHORIZING"
    )
    assert r.account_flat_verified is (n==0)
    assert r.owned_cross_positions==n
    assert r.native_market_stops_verified==n
    assert r.hedge_mode_verified is True
    assert r.inventory_bookends_verified
    assert r.immutable_ledger_bookends_verified
    assert r.incident_alert is False
    assert r.margin_and_liquidation_certified is False
    assert r.native_stop_exits_certified is False
    assert r.account_entry_allowed is False
    assert r.live_authorization is False
    assert r.mexc_posts==r.database_writes==0
    assert ledger.reads==2 if n==0 else ledger.reads==3
    assert venue.positions_reads==2 and venue.stop_reads==2
    assert complete.calls==1


@pytest.mark.parametrize("n",[0,2,4])
def test_any_missing_or_wrong_hedge_mode_halts_without_account_reads(n):
    r,ledger,venue,complete=watch(n,hedge_mode=0)
    assert r.status=="V40_FOUR_SLOT_SIGNED_HEDGE_MODE_UNVERIFIED"
    assert r.incident_alert
    assert ledger.reads==venue.positions_reads==venue.stop_reads==0


def test_any_extra_unowned_position_rejects_entire_four_slot_account():
    ledger,venue,complete=inputs(4)
    venue.first_positions.append(dict(venue.first_positions[0],positionId="999"))
    venue.last_positions.append(dict(venue.last_positions[0],positionId="999"))
    r=asyncio.run(_observe_verified_four_slot_sources(
        ledger=ledger,venue=venue,completeness=complete,hedge_mode=1,
    ))
    assert r.status=="V40_FOUR_SLOT_SIGNED_UNOWNED_OR_INCOMPLETE_ACCOUNT_HALT"
    assert r.incident_alert
    assert r.mexc_posts==0


def test_lost_native_sl_protection_rejects_all_four_shorts():
    ledger,venue,complete=inputs(4)
    for arr in (venue.first_stops,venue.last_stops):
        arr[2]["stopLossType"]=1
    r=asyncio.run(_observe_verified_four_slot_sources(
        ledger=ledger,venue=venue,completeness=complete,hedge_mode=1,
    ))
    assert r.status=="V40_FOUR_SLOT_SIGNED_UNPROTECTED_OR_UNKNOWN_OWNER_HALT"
    assert r.incident_alert and r.owned_cross_positions==0


def test_ledger_change_during_exchange_get_window_halts():
    ledger,venue,complete=inputs(3)
    ledger.last.checkpoints[1]["stop_price"]="123"
    r=asyncio.run(_observe_verified_four_slot_sources(
        ledger=ledger,venue=venue,completeness=complete,hedge_mode=1,
    ))
    assert r.status=="V40_FOUR_SLOT_SIGNED_UNOWNED_OR_INCOMPLETE_ACCOUNT_HALT"
    assert r.incident_alert


def test_ledger_changed_after_good_snapshot_halts_at_final_audit():
    class DriftingLedger(Ledger):
        async def read_snapshot(self):
            out=await super().read_snapshot()
            if self.reads==3:
                out.checkpoints[0]["stop_price"]="123"
            return out

    ld,venue,complete=inputs(4)
    ledger=DriftingLedger(ld.first)
    r=asyncio.run(_observe_verified_four_slot_sources(
        ledger=ledger,venue=venue,completeness=complete,hedge_mode=1,
    ))
    assert r.status=="V40_FOUR_SLOT_SIGNED_LEDGER_OR_VENUE_DRIFT_HALT"
    assert r.incident_alert


def test_no_unsigned_venue_or_unapproved_db_reader_can_access_gateway():
    class Unapproved:
        async def _private_post(self,*args):
            raise AssertionError("MEXC POST MUST NEVER BE AVAILABLE")
    result=asyncio.run(observe_dedicated_v40_four_slot_get_only(
        ledger=Unapproved(),signed_get=Unapproved(),
    ))
    assert result.status=="V40_FOUR_SLOT_SIGNED_READONLY_COMPONENTS_REQUIRED"
    assert result.incident_alert is True
    assert result.live_authorization is False


def test_closed_trade_history_flat_account_requires_proven_closed_ledger():
    ledger,venue,complete=inputs(0)
    ledger.first.intents.append({"state":"ACK","symbol":"OLD_USDT"})
    ledger.last.intents.append({"state":"ACK","symbol":"OLD_USDT"})
    result=asyncio.run(_observe_verified_four_slot_sources(
        ledger=ledger,venue=venue,completeness=complete,hedge_mode=1,
    ))
    assert result.status=="V40_FOUR_SLOT_SIGNED_UNOWNED_OR_INCOMPLETE_ACCOUNT_HALT"
    assert result.account_flat_verified is False
