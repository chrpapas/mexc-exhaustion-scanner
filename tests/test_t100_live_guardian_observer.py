"""GET-only owned guardian observer regressions, including stop-trigger race.

All exchange calls are mocked. No real API keys, POST, DB or Render.
"""
from __future__ import annotations

from decimal import Decimal as D
import unittest

from app.t100_live_owned_emergency_guardian import OwnedShort
from app.t100_live_guardian_observer import observe_owned_read_only
from app.t100_live_readonly_probe import ReadOnlyMexcProbe


SCOPE="c"*64


def owned():
    return OwnedShort(
        scope_sha256=SCOPE,symbol="BTC_USDT",position_id="555",
        entry_external_oid="t100v4_abcdef012345_entry",entry_order_id="666",
        contracts=D("1"),stop_order_id="777",stop_price=D("74500"),
        checkpoint_version=1,
    )


def active_stop(**changes):
    row=dict(symbol="BTC_USDT",positionId="555",id="777",
             orderId=0,positionType=2,state=1,isFinished=0,
             triggerSide=0,errorCode=0,stopLossPrice="74000",
             stopLossType=0,stopLossReverse=2,lossTrend=1,
             volType=2,profitLossVolType="SAME",
             vol=1,realityVol=1)
    row.update(changes)
    return row


def position():
    return dict(positionId="555",symbol="BTC_USDT",positionType=2,
                state=1,openType=1,holdVol="1",leverage=1)


def manual():
    return dict(positionId="222",symbol="ETH_USDT",positionType=1,
                state=1,openType=2,holdVol="3",leverage=3)


def manual_stop():
    return dict(positionId="222",symbol="ETH_USDT",id="888",
                state=1,stopLossPrice="2000")


def entry():
    return dict(symbol="BTC_USDT",positionId="555",
                orderId="666",externalOid="t100v4_abcdef012345_entry",
                side=3,state=3,openType=1,dealVol=1)


def history():
    return dict(positionId="555",symbol="BTC_USDT",
                positionType=2,openType=1,state=3,holdVol=0,
                closeVol=1,positionShowStatus="CLOSED")


class Venue:
    def __init__(self, *, own_active=True, own_stop=True, corrupt_stop=False,
                 history_closed=False, terminal_stop=False,
                 history_unavailable=False, wrong_entry=False):
        self.own_active=own_active
        self.own_stop=own_stop
        self.corrupt_stop=corrupt_stop
        self.history_closed=history_closed
        self.terminal_stop=terminal_stop
        self.history_unavailable=history_unavailable
        self.wrong_entry=wrong_entry
        self.gets=[]
        self.posts=[]

    async def _private_get(self,path,params=None):
        self.gets.append((path,params))
        if "/order/external/" in path:
            return entry()
        if path=="/api/v1/private/order/get/666":
            e=entry()
            if self.wrong_entry:
                e["dealVol"]=2
            return e
        if path=="/api/v1/private/order/open_order_total_count":
            k=(1 if self.own_stop and self.own_active else 0)+1
            return dict(sumCount=k,stopOrderCount=k,limitOrderCount=0,
                        planOrderCount=0,trackOrderCount=0)
        if path=="/api/v1/private/position/open_positions":
            if params is not None and "positionId" in params:
                return [position()] if self.own_active else []
            return ([position()] if self.own_active else [])+[manual()]
        if path=="/api/v1/private/stoporder/open_orders":
            stops=([active_stop(stopLossType=1 if self.corrupt_stop else 0)]
                   if self.own_stop and self.own_active else [])
            return stops+[manual_stop()]
        if path=="/api/v1/private/stoporder/list/orders":
            if self.history_unavailable:
                raise RuntimeError("finished history outage")
            return ([dict(id=777,positionId="555",symbol="BTC_USDT",
                          state=3,isFinished=1)]
                    if self.terminal_stop else [])
        if path=="/api/v1/private/position/list/history_positions":
            rows=[history()] if self.history_closed else []
            return dict(resultList=rows,pageSize=params["page_size"],
                        currentPage=params["page_num"],
                        totalCount=len(rows),totalPage=1 if rows else 0)
        raise AssertionError("UNEXPECTED_GET")

    async def _private_post(self,*args,**kwargs):
        self.posts.append((args,kwargs))
        raise AssertionError("POST_DISALLOWED")


class ObserverTests(unittest.IsolatedAsyncioTestCase):
    async def run_case(self,**kwargs):
        v=Venue(**kwargs)
        r=await observe_owned_read_only(owned=owned(),actual_scope=SCOPE,
                                        probe=ReadOnlyMexcProbe(v))
        self.assertFalse(r.close_allowed)
        self.assertFalse(r.entry_allowed)
        self.assertFalse(r.live_authorization)
        self.assertFalse(r.mexc_orders_sent)
        self.assertEqual(v.posts,[])
        self.assertEqual(r.foreign_position_count,1)
        return r,v

    async def test_native_full_market_stop_is_observed_without_authorizing(self):
        r,v=await self.run_case()
        self.assertEqual(r.status,"OWNED_NATIVE_MARKET_STOP_CURRENTLY_OBSERVED")
        self.assertTrue(r.native_market_stop_verified)
        self.assertFalse(r.alert)

    async def test_stop_id_exists_but_limit_type_blocks_protection(self):
        r,_=await self.run_case(corrupt_stop=True)
        self.assertEqual(r.status,"OWNED_STOP_PRESENT_BUT_PROTECTION_UNVERIFIED")
        self.assertTrue(r.alert)
        self.assertFalse(r.native_market_stop_verified)

    async def test_missing_stop_may_be_trigger_in_progress(self):
        r,_=await self.run_case(own_stop=False,terminal_stop=True)
        self.assertEqual(r.status,"OWNED_STOP_MISSING_TERMINAL_TRIGGER_MAY_BE_IN_FLIGHT")
        self.assertTrue(r.alert)
        self.assertFalse(r.close_allowed)

    async def test_missing_stop_nonexhaustive_history_is_not_permission(self):
        r,_=await self.run_case(own_stop=False)
        self.assertEqual(r.status,"OWNED_STOP_MISSING_TRIGGER_STATUS_UNCERTAIN")
        self.assertTrue(r.alert)

    async def test_history_unavailable_is_not_permission_to_close(self):
        r,_=await self.run_case(own_stop=False,history_unavailable=True)
        self.assertEqual(r.status,"OWNED_STOP_MISSING_FINISHED_HISTORY_UNAVAILABLE")
        self.assertTrue(r.alert)

    async def test_historically_closed_exact_owned_position_not_account_flat(self):
        r,_=await self.run_case(own_active=False,own_stop=False,
                                history_closed=True)
        self.assertEqual(r.status,"OWNED_POSITION_CLOSED_IN_EXCHANGE_HISTORY")
        self.assertFalse(r.alert)
        self.assertFalse(r.live_authorization)
        self.assertFalse(r.entry_allowed)

    async def test_owned_absent_no_closed_history_is_uncertified(self):
        r,_=await self.run_case(own_active=False,own_stop=False)
        self.assertEqual(r.status,"OWNED_POSITION_NOT_SEEN_HISTORY_NOT_CLOSED_PROVEN")
        self.assertTrue(r.alert)

    async def test_wrong_entry_order_cannot_adopt_position(self):
        v=Venue(wrong_entry=True)
        r=await observe_owned_read_only(owned=owned(),actual_scope=SCOPE,
                                        probe=ReadOnlyMexcProbe(v))
        self.assertEqual(r.status,"ENTRY_FILLED_OWNERSHIP_UNVERIFIED")
        self.assertFalse(r.close_allowed)
        self.assertEqual(v.posts,[])

    async def test_wrong_account_scope_halts_before_any_get(self):
        v=Venue()
        with self.assertRaises(Exception):
            await observe_owned_read_only(owned=owned(),actual_scope="d"*64,
                                          probe=ReadOnlyMexcProbe(v))
        self.assertEqual(v.gets,[])


if __name__=="__main__":
    unittest.main()
