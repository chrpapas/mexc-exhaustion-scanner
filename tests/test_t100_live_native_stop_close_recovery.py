"""Native-stop GET-only recovery tests, including real V5 TP-side conflict.

No real exchange calls, no DB, no orders, no modifications of manual inventory.
"""
from __future__ import annotations

from decimal import Decimal as D
import unittest

from app.t100_live_native_stop_close_recovery import (
    recover_native_stop_close_read_only,
)
from app.t100_live_readonly_probe import ReadOnlyMexcProbe
from app.t100_live_owned_emergency_guardian import OwnedShort


SCOPE = "a"*64


def own():
    return OwnedShort(
        scope_sha256=SCOPE, symbol="BTC_USDT",
        position_id="321", entry_external_oid="t100v4_abcd12345678_entry",
        entry_order_id="456", contracts=D("1"),
        stop_order_id="123", stop_price=D("70000"),
        checkpoint_version=1,
    )


def entry(**kw):
    r=dict(orderId="456",externalOid=own().entry_external_oid,
           positionId="321",symbol="BTC_USDT",side=3,state=3,
           dealVol="1",dealAvgPrice="69000")
    r.update(kw)
    return r


def stop(**kw):
    r=dict(id="123",orderId=0,positionId="321",
           symbol="BTC_USDT",state=3,isFinished=1,
           positionType=2,stopLossType=0,volType=2,
           stopLossReverse=2,lossTrend=1,errorCode=0,
           profitLossVolType="SAME",stopLossPrice="70000",
           realityVol="1",placeOrderId="789",triggerSide=1)
    r.update(kw)
    return r


def close(**kw):
    r=dict(orderId="789",positionId="321",symbol="BTC_USDT",
           side=2,state=3,orderType=5,errorCode=0,
           dealVol="1",dealAvgPrice="70100")
    r.update(kw)
    return r


def historical(**kw):
    r=dict(positionId=321,symbol="BTC_USDT",positionType=2,
           openType=1,state=3,holdVol=0,closeVol=1,
           positionShowStatus="CLOSED")
    r.update(kw)
    return r


class FakeVenue:
    def __init__(self, *, entry_row=None, stop_rows=None,
                 close_row=None, history_rows=None,
                 open_exact=None, change_stop=False):
        self.entry_row=entry() if entry_row is None else entry_row
        self.stop_rows=[stop()] if stop_rows is None else stop_rows
        self.close_row=close() if close_row is None else close_row
        self.history_rows=[historical()] if history_rows is None else history_rows
        self.open_exact=[] if open_exact is None else open_exact
        self.change_stop=change_stop
        self.calls=[]
        self.posts=[]
        self.stop_reads=0

    async def _private_get(self,path,params=None):
        self.calls.append((path,params))
        if path.startswith("/api/v1/private/order/external/"):
            return dict(self.entry_row)
        if path=="/api/v1/private/order/get/456":
            return dict(self.entry_row)
        if path=="/api/v1/private/order/get/789":
            return dict(self.close_row)
        if path=="/api/v1/private/stoporder/list/orders":
            self.stop_reads+=1
            if self.change_stop and self.stop_reads>1:
                return [stop(triggerSide=2)]
            return [dict(x) for x in self.stop_rows]
        if path=="/api/v1/private/position/list/history_positions":
            return [dict(x) for x in self.history_rows]
        if path=="/api/v1/private/position/open_positions":
            if params is not None and "positionId" in params:
                return [dict(x) for x in self.open_exact]
            raise AssertionError("Must use exact position-ID filter")
        raise AssertionError("UNEXPECTED_GET_ENDPOINT")

    async def _private_post(self,*args,**kwargs):
        self.posts.append((args,kwargs))
        raise AssertionError("POST_NOT_ALLOWED")


class RecoveryTests(unittest.IsolatedAsyncioTestCase):
    async def check(self,venue,scope=SCOPE):
        r=await recover_native_stop_close_read_only(
            owned=own(),actual_scope=scope,
            probe=ReadOnlyMexcProbe(venue))
        self.assertFalse(r.live_authorization)
        self.assertFalse(r.emergency_close_allowed)
        self.assertFalse(r.entry_allowed)
        self.assertFalse(r.manual_position_management_allowed)
        self.assertFalse(r.whole_account_flat_certified)
        self.assertFalse(r.stop_loss_trigger_side_proven)
        self.assertEqual(venue.posts,[])
        for secret in ("321","456","789","70000"):
            self.assertNotIn(secret,str(r))
        return r

    async def test_real_v5_pattern_filled_close_and_positive_array_history(self):
        v=FakeVenue(history_rows=[
            historical(positionId=311),historical(positionId=312),
            historical(),historical(positionId=314)])
        r=await self.check(v)
        self.assertEqual(r.status,
            "NATIVE_STOP_CLOSE_AND_OWNED_POSITION_VERIFIED_VENUE_TP_LABEL_CONFLICT")
        self.assertTrue(r.entry_filled_verified)
        self.assertTrue(r.native_stop_terminal_verified)
        self.assertTrue(r.linked_venue_close_filled)
        self.assertTrue(r.owned_position_closed_verified)
        self.assertTrue(r.venue_trigger_side_conflict)
        self.assertFalse(r.venue_sl_label_observed)
        self.assertFalse(r.historical_pages_exhausted)
        self.assertEqual(len(v.calls),9) # 2 entry + 2 stop + 1 close + 4 history/open

    async def test_correct_venue_sl_label_does_not_bypass_independence(self):
        v=FakeVenue(stop_rows=[stop(triggerSide=2)])
        r=await self.check(v)
        self.assertEqual(r.status,
                         "NATIVE_STOP_CLOSE_AND_OWNED_POSITION_VERIFIED_SL_LABEL")
        self.assertTrue(r.venue_sl_label_observed)
        self.assertFalse(r.stop_loss_trigger_side_proven)

    async def test_foreign_manual_row_in_history_is_not_target(self):
        v=FakeVenue(history_rows=[
            historical(positionId=999),historical()])
        r=await self.check(v)
        self.assertTrue(r.owned_position_closed_verified)
        self.assertFalse(r.manual_position_management_allowed)

    async def test_missing_stop_page_does_not_prove_no_stop(self):
        v=FakeVenue(stop_rows=[])
        r=await self.check(v)
        self.assertEqual(r.status,"EXACT_FINISHED_STOP_NOT_OBSERVED_ON_BOTH_PAGES")
        self.assertFalse(r.linked_venue_close_filled)

    async def test_ambiguous_stop_pages_or_identity_drift_fail_closed(self):
        for venue in (
            FakeVenue(stop_rows=[stop(),stop()]),
            FakeVenue(change_stop=True),
            FakeVenue(stop_rows=[stop(positionId="999")]),
            FakeVenue(stop_rows=[stop(stopLossType=1)]),
            FakeVenue(stop_rows=[stop(realityVol="0.5")]),
            FakeVenue(stop_rows=[stop(placeOrderId="0")]),
            FakeVenue(stop_rows=[stop(state=1)]),
            FakeVenue(stop_rows=[stop(takeProfitPrice="80000")]),
        ):
            with self.subTest(stop_rows=venue.stop_rows):
                r=await self.check(venue)
                self.assertFalse(r.owned_position_closed_verified)

    async def test_missing_entry_and_wrong_origin_block_stop_reads(self):
        for entry_row in (entry(side=2),entry(dealVol="0.5"),
                          entry(positionId="999"),entry(externalOid="manual"),
                          entry(state=2)):
            with self.subTest(entry_row=entry_row):
                v=FakeVenue(entry_row=entry_row)
                r=await self.check(v)
                self.assertFalse(r.entry_filled_verified)
                self.assertEqual(v.stop_reads,0)

    async def test_close_partial_or_wrong_target_never_certifies(self):
        for change in (
            {"side":3},{"state":2},{"orderType":1},{"dealVol":"0.5"},
            {"positionId":"999"},{"symbol":"ETH_USDT"},{"errorCode":1},
            {"orderId":"888"},
        ):
            with self.subTest(change=change):
                r=await self.check(FakeVenue(close_row=close(**change)))
                self.assertFalse(r.linked_venue_close_filled)
                self.assertFalse(r.owned_position_closed_verified)

    async def test_historical_owned_id_absent_is_uncertified_not_closed(self):
        v=FakeVenue(history_rows=[historical(positionId=999)])
        r=await self.check(v)
        self.assertEqual(r.status,"LINKED_CLOSE_FILLED_OWNED_CLOSURE_NOT_PROVEN")
        self.assertTrue(r.linked_venue_close_filled)
        self.assertFalse(r.owned_position_closed_verified)

    async def test_open_exact_position_defeats_positive_terminal_history(self):
        v=FakeVenue(open_exact=[dict(positionId=321,symbol="BTC_USDT")])
        r=await self.check(v)
        self.assertEqual(r.status,"LINKED_CLOSE_FILLED_OWNED_CLOSURE_NOT_PROVEN")
        self.assertFalse(r.owned_position_closed_verified)

    async def test_wrong_account_scope_halts_before_any_get(self):
        v=FakeVenue()
        with self.assertRaises(Exception):
            await recover_native_stop_close_read_only(
                owned=own(),actual_scope="b"*64,
                probe=ReadOnlyMexcProbe(v))
        self.assertEqual(v.calls,[])


if __name__=="__main__":
    unittest.main()
