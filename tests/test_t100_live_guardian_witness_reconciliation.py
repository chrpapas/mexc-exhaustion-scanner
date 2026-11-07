"""Fail-closed MEXC signed-GET inventory and owned close reconciliation tests.

No real MEXC, network, database, secrets, Render, or order mutations.
"""
from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal as D
import unittest

from app.t100_live_owned_emergency_guardian import (
    OwnedShort, OwnedGuardianHalted, close_intent_oid,
)
from app.t100_live_readonly_probe import ReadOnlyMexcProbe
from app.t100_live_guardian_inventory_witness import (
    ReadOnlyGuardianCollector, GuardianWitnessHalted,
)
from app.t100_live_owned_close_reconciliation import (
    CloseReconciliationHalted, reconcile_close_read_only,
)

SCOPE = "c" * 64


def owned():
    return OwnedShort(
        scope_sha256=SCOPE, symbol="BTC_USDT",
        position_id="555", entry_external_oid="t100v4_abcdef012345_entry",
        entry_order_id="666", contracts=D("1"),
        stop_order_id="777", stop_price=D("74500"),
        checkpoint_version=1,
    )


def own_position():
    return dict(symbol="BTC_USDT", positionId="555", positionType=2,
                state=1, holdVol="1", openType=1, leverage=1)


def manual_position():
    return dict(symbol="ETH_USDT", positionId="222", positionType=1,
                state=1, holdVol="4", openType=2, leverage=4)


def manual_stop():
    return dict(symbol="ETH_USDT", positionId="222", id="888",
                state=1, stopLossPrice="2500")


def own_stop():
    return dict(symbol="BTC_USDT", positionId="555", id="777",
                state=1, stopLossPrice="74500")


def counts(stop_count=1):
    return dict(sumCount=stop_count,stopOrderCount=stop_count,
                limitOrderCount=0,planOrderCount=0,trackOrderCount=0)


def close_order(o=None):
    o = o or owned()
    oid = close_intent_oid(o)
    return dict(orderId="999", positionId="555",
                symbol="BTC_USDT", externalOid=oid,
                side=2, state=3, orderType=5,
                dealVol="1", vol=1, openType=1,
                positionMode=1, errorCode=0)


class Venue:
    def __init__(self, *, positions=None, stops=None, n=None,
                 order=None, external_missing=False, drift=False):
        self.positions = [manual_position()] if positions is None else positions
        self.stops = [manual_stop()] if stops is None else stops
        self.counts = counts() if n is None else n
        self.order = close_order() if order is None else order
        self.external_missing = external_missing
        self.drift = drift
        self.calls = []
        self.post_calls = []

    async def _private_get(self, path, params=None):
        self.calls.append((path, params))
        if path == "/api/v1/private/order/open_order_total_count":
            val = dict(self.counts)
            if self.drift and sum(
                x[0] == path for x in self.calls) > 1:
                val = counts(2)
            return val
        if path == "/api/v1/private/position/open_positions":
            return [dict(x) for x in self.positions]
        if path == "/api/v1/private/stoporder/open_orders":
            return [dict(x) for x in self.stops]
        if path.startswith("/api/v1/private/order/external/BTC_USDT/"):
            return None if self.external_missing else dict(self.order)
        if path == "/api/v1/private/order/get/999":
            return dict(self.order)
        raise AssertionError(f"Unexpected endpoint: {path}")

    async def _private_post(self, *args, **kwargs):
        self.post_calls.append((args, kwargs))
        raise AssertionError("POST_NOT_ALLOWED")


class Ledger:
    def __init__(self, state="UNKNOWN", fail=False):
        self.state = state
        self.fail = fail
        self.calls = []

    async def read_state(self, oid, position):
        self.calls.append((oid, position.position_id))
        if self.fail:
            raise RuntimeError("NO_DATABASE")
        return self.state

    async def transition(self, *args, **kwargs):
        raise AssertionError("No journal write allowed")


class WitnessTests(unittest.IsolatedAsyncioTestCase):
    async def test_signed_get_double_read_with_manual_position(self):
        venue = Venue()
        witness = await ReadOnlyGuardianCollector(
            ReadOnlyMexcProbe(venue)
        ).observe(owned(), SCOPE)
        self.assertTrue(witness.critical_snapshots_consistent)
        self.assertTrue(witness.stop_total_reconciled)
        self.assertEqual(witness.foreign_position_count, 1)
        self.assertFalse(witness.exact_owned_position_present)
        self.assertFalse(witness.position_completeness_certified)
        self.assertFalse(witness.emergency_close_authorized)
        self.assertFalse(witness.live_authorization)
        self.assertEqual(len(venue.calls), 8)
        self.assertEqual(venue.post_calls, [])

    async def test_present_owned_plus_manual_positions_are_separate(self):
        venue = Venue(positions=[own_position(),manual_position()],
                      stops=[own_stop(),manual_stop()],n=counts(2))
        w = await ReadOnlyGuardianCollector(ReadOnlyMexcProbe(venue)).observe(owned(),SCOPE)
        self.assertTrue(w.exact_owned_position_present)
        self.assertTrue(w.owned_native_stop_present)
        self.assertEqual(w.foreign_position_count, 1)

    async def test_pnl_drift_tolerated_without_false_certificate(self):
        class PnlVenue(Venue):
            async def _private_get(self, path, params=None):
                value=await super()._private_get(path,params)
                if path.endswith("/open_positions"):
                    value[0]["unRealizedPnl"] = str(len(self.calls))
                return value
        w=await ReadOnlyGuardianCollector(ReadOnlyMexcProbe(PnlVenue())).observe(owned(),SCOPE)
        self.assertTrue(w.critical_snapshots_consistent)
        self.assertFalse(w.emergency_close_authorized)

    async def test_mismatched_stop_counts_halts(self):
        venue=Venue(n=counts(2))
        with self.assertRaisesRegex(GuardianWitnessHalted,"STOP_COUNT_MISMATCH"):
            await ReadOnlyGuardianCollector(ReadOnlyMexcProbe(venue)).observe(owned(),SCOPE)
        self.assertEqual(venue.post_calls,[])

    async def test_malformed_counts_and_duplicate_position_block(self):
        for v in (
            Venue(n=dict(counts(),sumCount=2)),
            Venue(positions=[manual_position(),manual_position()]),
            Venue(stops=[manual_stop(),manual_stop()], n=counts(2)),
            Venue(drift=True),
        ):
            with self.subTest(v=v):
                with self.assertRaises((GuardianWitnessHalted,Exception)):
                    await ReadOnlyGuardianCollector(ReadOnlyMexcProbe(v)).observe(owned(),SCOPE)
                self.assertFalse(v.post_calls)

    async def test_wrong_account_scope_blocks_reads(self):
        v=Venue()
        with self.assertRaises(OwnedGuardianHalted):
            await ReadOnlyGuardianCollector(ReadOnlyMexcProbe(v)).observe(owned(),"d"*64)
        self.assertEqual(v.calls,[])

    async def test_unknown_endpoint_shape_fails_closed(self):
        class BadVenue(Venue):
            async def _private_get(self,path,params=None):
                val=await super()._private_get(path,params)
                if path.endswith("open_positions"):
                    return {"resultList":val} # wrapper not proven complete
                return val
        with self.assertRaises(GuardianWitnessHalted):
            await ReadOnlyGuardianCollector(ReadOnlyMexcProbe(BadVenue())).observe(owned(),SCOPE)


class ReconciliationTests(unittest.IsolatedAsyncioTestCase):
    async def test_exact_filled_order_and_two_absent_reads_is_still_uncertified(self):
        v=Venue()
        ledger=Ledger()
        r=await reconcile_close_read_only(
            owned=owned(),actual_scope=SCOPE,ledger=ledger,
            probe=ReadOnlyMexcProbe(v))
        self.assertEqual(r.status,"MATCHED_CLOSE_FILLED_POSITION_HISTORY_UNVERIFIED")
        self.assertTrue(r.order_filled_verified)
        self.assertTrue(r.owned_position_not_seen_twice)
        self.assertEqual(r.foreign_positions_preserved_observed,1)
        self.assertTrue(r.ledger_reconcile_only)
        self.assertFalse(r.live_authorization)
        self.assertEqual(len(ledger.calls),1)
        self.assertEqual(v.post_calls,[])

    async def test_owned_position_still_visible_no_false_flat(self):
        v=Venue(positions=[manual_position(),own_position()])
        r=await reconcile_close_read_only(
            owned=owned(),actual_scope=SCOPE,ledger=Ledger(),
            probe=ReadOnlyMexcProbe(v))
        self.assertEqual(r.status,"FILLED_CLOSE_BUT_OWNED_POSITION_STILL_VISIBLE")
        self.assertTrue(r.order_filled_verified)
        self.assertFalse(r.owned_position_not_seen_twice)

    async def test_no_external_id_is_unknown_even_when_no_positions(self):
        v=Venue(external_missing=True)
        r=await reconcile_close_read_only(
            owned=owned(),actual_scope=SCOPE,ledger=Ledger(),
            probe=ReadOnlyMexcProbe(v))
        self.assertEqual(r.status,"CLOSE_OID_NOT_FOUND_OUTCOME_UNKNOWN")
        self.assertFalse(r.order_filled_verified)
        self.assertEqual(v.post_calls,[])

    async def test_intent_prepared_does_not_imply_submission(self):
        v=Venue()
        r=await reconcile_close_read_only(
            owned=owned(),actual_scope=SCOPE,ledger=Ledger(state="PREPARED"),
            probe=ReadOnlyMexcProbe(v))
        self.assertEqual(r.status,"NO_SUBMISSION_CLAIM_TO_RECONCILE")
        self.assertEqual(v.calls,[])

    async def test_bad_mexc_close_fields_refuse_verified_fill(self):
        valid=close_order()
        for key,value in (
            ("externalOid","manual"),
            ("positionId","222"),
            ("symbol","ETH_USDT"),
            ("orderId","333"),
            ("side",3),
            ("state",2),
            ("orderType",1),
            ("openType",2),
            ("positionMode",2),
            ("errorCode",1),
            ("dealVol","0.5"),
            ("vol",2),
        ):
            with self.subTest(field=key):
                bad=dict(valid,**{key:value})
                v=Venue(order=bad)
                if key in {"externalOid", "symbol", "orderId"}:
                    # Strict GET identity mismatch is a HARD HALT, not a
                    # soft nonverification result. No mutation can follow.
                    with self.assertRaises(CloseReconciliationHalted):
                        await reconcile_close_read_only(
                            owned=owned(),actual_scope=SCOPE,ledger=Ledger(),
                            probe=ReadOnlyMexcProbe(v))
                else:
                    r=await reconcile_close_read_only(
                        owned=owned(),actual_scope=SCOPE,ledger=Ledger(),
                        probe=ReadOnlyMexcProbe(v))
                    self.assertFalse(r.order_filled_verified)
                self.assertEqual(v.post_calls,[])

    async def test_unverified_inventory_after_filled_close_cannot_certify_flat(self):
        v=Venue(n=counts(3))
        r=await reconcile_close_read_only(
            owned=owned(),actual_scope=SCOPE,ledger=Ledger(),
            probe=ReadOnlyMexcProbe(v))
        self.assertEqual(r.status,"FILLED_CLOSE_BUT_INVENTORY_WITNESS_UNVERIFIED")
        self.assertTrue(r.order_filled_verified)
        self.assertFalse(r.owned_position_not_seen_twice)

    async def test_database_unavailable_fails_before_exchange(self):
        v=Venue()
        with self.assertRaisesRegex(CloseReconciliationHalted,"LEDGER"):
            await reconcile_close_read_only(
                owned=owned(),actual_scope=SCOPE,
                ledger=Ledger(fail=True),probe=ReadOnlyMexcProbe(v))
        self.assertEqual(v.calls,[])

    async def test_no_writes_for_unknown_or_verified_state(self):
        for state in ("SUBMITTING","UNKNOWN","VERIFIED"):
            with self.subTest(state=state):
                v=Venue()
                r=await reconcile_close_read_only(
                    owned=owned(),actual_scope=SCOPE,
                    ledger=Ledger(state=state),probe=ReadOnlyMexcProbe(v))
                self.assertTrue(r.ledger_reconcile_only)
                self.assertEqual(v.post_calls,[])


if __name__ == "__main__":
    unittest.main()
