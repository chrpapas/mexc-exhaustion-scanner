"""Offline adversarial closed-position finality tests. Zero POST or DB writes."""
from __future__ import annotations

from decimal import Decimal as D
import unittest

from app.t100_live_owned_emergency_guardian import OwnedShort, close_intent_oid
from app.t100_live_owned_position_finality import (
    verify_owned_position_finality, OwnedPositionFinalityHalted,
)
from app.t100_live_readonly_probe import ReadOnlyMexcProbe
from app.t100_live_owned_close_reconciliation import reconcile_close_read_only


SCOPE = "c" * 64


def own():
    return OwnedShort(
        scope_sha256=SCOPE, symbol="BTC_USDT", position_id="555",
        entry_external_oid="t100v4_abcdef012345_entry",
        entry_order_id="666", contracts=D("1"),
        stop_order_id="777", stop_price=D("74500"),
        checkpoint_version=1,
    )


def historical(**kw):
    x = dict(positionId=555, symbol="BTC_USDT", positionType=2,
             openType=1, state=3, holdVol=0, closeVol=1,
             positionShowStatus="CLOSED", updateTime=1762370000000)
    x.update(kw)
    return x


def manual_pos(**kw):
    x=dict(symbol="ETH_USDT", positionId=222, state=1,
           positionType=1, holdVol=3, openType=2)
    x.update(kw)
    return x


class FakeVenue:
    def __init__(self, *, rows=None, active=None, page_error=None,
                 drift=False, mismatched_exact=False, order=None):
        self.rows = [historical()] if rows is None else rows
        self.active = [] if active is None else active
        self.page_error = page_error
        self.drift = drift
        self.mismatched_exact = mismatched_exact
        self.order = order or self._order()
        self.calls=[]
        self.writes=0
        self.history_reads=0

    def _order(self):
        return dict(orderId="999", symbol="BTC_USDT", positionId="555",
                    externalOid=close_intent_oid(own()), side=2, state=3,
                    orderType=5, openType=1, positionMode=1,
                    errorCode=0, vol=1, dealVol=1)

    async def _private_get(self, path, params=None):
        self.calls.append((path, params))
        if path == "/api/v1/private/position/list/history_positions":
            p = params["page_num"]; sz=params["page_size"]
            self.history_reads += 1
            data = self.rows
            if self.drift and self.history_reads > 1:
                data = [dict(self.rows[0], closeVol="0.5")]
            start=(p-1)*sz
            out=dict(pageSize=sz,totalCount=len(data),
                     totalPage=(len(data)+sz-1)//sz,
                     currentPage=p,resultList=data[start:start+sz])
            if self.page_error is not None:
                out.update(self.page_error)
            return out
        if path == "/api/v1/private/position/open_positions":
            if params is not None and "positionId" in params:
                return ([manual_pos()] if self.mismatched_exact else
                        [dict(x) for x in self.active])
            return [manual_pos()]  # Manual portfolio exposure may remain.
        if path == "/api/v1/private/stoporder/open_orders":
            return [{"id": 888, "positionId": 222, "symbol": "ETH_USDT"}]
        if path == "/api/v1/private/order/open_order_total_count":
            return dict(sumCount=1,stopOrderCount=1,limitOrderCount=0,
                        planOrderCount=0,trackOrderCount=0)
        if path.startswith("/api/v1/private/order/external/"):
            return dict(self.order)
        if path == "/api/v1/private/order/get/999":
            return dict(self.order)
        raise AssertionError(f"Unexpected GET endpoint {path}")

    async def _private_post(self,*args,**kwargs):
        self.writes += 1
        raise AssertionError("POST prohibited in all finality tests")


class Ledger:
    async def read_state(self, oid, owned):
        if oid != close_intent_oid(owned):
            raise AssertionError("wrong ownership")
        return "UNKNOWN"

    async def transition(self, *args, **kwargs):
        raise AssertionError("ledger must remain read-only")


class FinalityTests(unittest.IsolatedAsyncioTestCase):
    async def test_exact_closed_history_repeated_and_absent_exact_position(self):
        v=FakeVenue()
        x=await verify_owned_position_finality(
            owned=own(),actual_scope=SCOPE,probe=ReadOnlyMexcProbe(v))
        self.assertEqual(x.status, "OWNED_POSITION_CLOSED_EXACT_HISTORY_AND_GET_VERIFIED")
        self.assertTrue(x.closed_position_verified)
        self.assertTrue(x.open_position_absent_twice)
        self.assertTrue(x.historical_pages_exhausted)
        self.assertFalse(x.account_flat_certified)
        self.assertFalse(x.live_authorization)
        self.assertFalse(x.emergency_close_authorized)
        self.assertEqual(v.writes,0)
        self.assertEqual(sum("/position/list/history_positions" in p for p,_ in v.calls),2)
        self.assertEqual(sum(p.endswith("/open_positions") for p,_ in v.calls),2)

    async def test_closure_does_not_adopt_existing_manual_position(self):
        v=FakeVenue()
        x=await reconcile_close_read_only(
            owned=own(),actual_scope=SCOPE,
            ledger=Ledger(),probe=ReadOnlyMexcProbe(v))
        self.assertEqual(x.status,"EXACT_OWNED_CLOSE_AND_CLOSED_POSITION_HISTORY_VERIFIED")
        self.assertTrue(x.owned_position_closure_certified)
        self.assertTrue(x.order_filled_verified)
        self.assertEqual(x.foreign_positions_preserved_observed,1)
        self.assertFalse(x.live_authorization)
        self.assertTrue(x.ledger_reconcile_only)
        self.assertEqual(v.writes,0)

    async def test_true_history_absent_does_not_infer_owned_closed(self):
        v=FakeVenue(rows=[])
        x=await verify_owned_position_finality(
            owned=own(),actual_scope=SCOPE,probe=ReadOnlyMexcProbe(v))
        self.assertFalse(x.closed_position_verified)
        self.assertEqual(x.status,"EXACT_CLOSED_POSITION_HISTORY_NOT_FOUND")

    async def test_own_position_still_active_not_final(self):
        v=FakeVenue(active=[historical(state=1,holdVol=1,closeVol=0)])
        x=await verify_owned_position_finality(
            owned=own(),actual_scope=SCOPE,probe=ReadOnlyMexcProbe(v))
        self.assertEqual(x.status,"EXACT_OWNED_POSITION_STILL_OPEN")

    async def test_wrong_position_history_never_substituted(self):
        v=FakeVenue(rows=[historical(positionId=556)])
        x=await verify_owned_position_finality(
            owned=own(),actual_scope=SCOPE,probe=ReadOnlyMexcProbe(v))
        self.assertFalse(x.closed_position_verified)

    async def test_partial_close_liquidation_side_or_margin_not_accepted(self):
        for bad in (
            dict(closeVol="0.5"), dict(holdVol=1),
            dict(state=2), dict(positionType=1),
            dict(openType=2), dict(positionShowStatus="LIQUIDATED"),
            dict(symbol="ETH_USDT")
        ):
            with self.subTest(bad=bad):
                v=FakeVenue(rows=[historical(**bad)])
                if "symbol" in bad:
                    with self.assertRaises(OwnedPositionFinalityHalted):
                        await verify_owned_position_finality(
                            owned=own(),actual_scope=SCOPE,
                            probe=ReadOnlyMexcProbe(v))
                else:
                    x=await verify_owned_position_finality(
                        owned=own(),actual_scope=SCOPE,
                        probe=ReadOnlyMexcProbe(v))
                    self.assertFalse(x.closed_position_verified)

    async def test_historical_page_metadata_must_match_and_be_complete(self):
        for bad in (
            {"pageSize": 25},{"totalCount":5},{"totalPage":2},
            {"currentPage":0},{"totalCount":"1"},
            {"resultList":"not_rows"},
        ):
            with self.subTest(bad=bad):
                v=FakeVenue(page_error=bad)
                with self.assertRaises(OwnedPositionFinalityHalted):
                    await verify_owned_position_finality(
                        owned=own(),actual_scope=SCOPE,
                        probe=ReadOnlyMexcProbe(v))

    async def test_all_pages_required_and_no_page_limit_bypass(self):
        rows=[historical(positionId=i+500) for i in range(4)]
        rows[2]=historical()
        v=FakeVenue(rows=rows)
        x=await verify_owned_position_finality(
            owned=own(),actual_scope=SCOPE,
            probe=ReadOnlyMexcProbe(v),page_size=2,max_pages=3)
        self.assertTrue(x.closed_position_verified)
        self.assertEqual(v.history_reads,4)

    async def test_page_limit_fails_closed(self):
        rows=[historical(positionId=i+500) for i in range(6)]
        rows[3]=historical()
        v=FakeVenue(rows=rows)
        with self.assertRaisesRegex(OwnedPositionFinalityHalted,"PAGE_LIMIT"):
            await verify_owned_position_finality(
                owned=own(),actual_scope=SCOPE,
                probe=ReadOnlyMexcProbe(v),page_size=2,max_pages=2)

    async def test_historical_page_repetition_duplicate_or_changing_record_fails(self):
        v=FakeVenue(rows=[historical(),historical()])
        with self.assertRaisesRegex(OwnedPositionFinalityHalted,"DUPLICATE"):
            await verify_owned_position_finality(
                owned=own(),actual_scope=SCOPE,probe=ReadOnlyMexcProbe(v))
        v=FakeVenue(drift=True)
        x=await verify_owned_position_finality(
            owned=own(),actual_scope=SCOPE,probe=ReadOnlyMexcProbe(v))
        self.assertFalse(x.closed_position_verified)
        self.assertEqual(x.status,"EXACT_CLOSED_POSITION_HISTORY_NOT_FOUND")

    async def test_exact_position_filter_bug_hard_halts(self):
        v=FakeVenue(mismatched_exact=True)
        with self.assertRaises(OwnedPositionFinalityHalted):
            await verify_owned_position_finality(
                owned=own(),actual_scope=SCOPE,probe=ReadOnlyMexcProbe(v))

    async def test_wrong_scope_halts_before_get(self):
        v=FakeVenue()
        with self.assertRaises(Exception):
            await verify_owned_position_finality(
                owned=own(),actual_scope="d"*64,probe=ReadOnlyMexcProbe(v))
        self.assertEqual(v.calls,[])


if __name__ == "__main__":
    unittest.main()
