"""Read-only independent pilot exposure observer tests; never send orders."""
from __future__ import annotations

import asyncio
import copy
import time
import unittest
from decimal import Decimal

from app.t100_live_readonly_probe import ReadOnlyMexcProbe
from app.t100_pilot_watchdog_observer import (
    PilotWatchScope,
    assess_pilot_watch,
    observe_pilot_watch_get,
)

_ACCOUNT = "a" * 64


def scope(**overrides):
    kwargs = {
        "account": _ACCOUNT, "origin_oid": "pilot-owned-entry",
        "stop_id": "99", "maximum_stop_price": Decimal("110"),
        "alert_after_ms": 3000,
    }
    kwargs.update(overrides)
    return PilotWatchScope(**kwargs)


def evidence(*, protected=True, flat=False):
    now = int(time.time() * 1000)
    origin = {
        "orderId": "100", "positionId": "900",
        "externalOid": "pilot-owned-entry", "symbol": "BTC_USDT",
        "state": 3, "side": 3, "dealVol": "1",
    }
    deal = {
        "id": "201", "orderId": "100", "symbol": "BTC_USDT",
        "side": 3, "vol": "1", "price": "100", "timestamp": now - 2000,
    }
    p = {
        "positionId": "900", "symbol": "BTC_USDT", "holdVol": "1",
        "positionType": 2, "openType": 1, "leverage": 1,
    }
    s = {
        "id": "99", "positionId": "900", "symbol": "BTC_USDT",
        "orderId": 0, "state": 1, "isFinished": 0,
        "triggerSide": 0, "errorCode": 0, "positionType": 2,
        "vol": "1", "volType": 2, "profitLossVolType": "SAME",
        "stopLossPrice": "109", "stopLossType": 0,
        "stopLossReverse": 2, "lossTrend": 1,
    }
    before = {
        "positions": [] if flat else [p],
        "stops": [s] if protected and not flat else [],
        "counts": {
            "sumCount": 1 if protected and not flat else 0,
            "stopOrderCount": 1 if protected and not flat else 0,
            "limitOrderCount": 0, "planOrderCount": 0, "trackOrderCount": 0,
        },
    }
    return {
        "account": _ACCOUNT,
        "start_ms": now - 300, "end_ms": now - 100,
        "before": before, "after": copy.deepcopy(before),
        "origin": origin, "origin_bookend": copy.deepcopy(origin),
        "deals": [deal], "deals_bookend": [copy.deepcopy(deal)],
    }


class ObserverPolicyTests(unittest.TestCase):
    def check(self, ev, sc=None):
        return assess_pilot_watch(sc or scope(), ev, now_ms=int(time.time()*1000))

    def test_one_owned_active_native_market_stop_currently_observed(self):
        result = self.check(evidence())
        self.assertEqual(result.status, "WATCH_NATIVE_STOP_CURRENTLY_OBSERVED_NONAUTHORIZING")
        self.assertTrue(result.currently_active_stop_observed)
        self.assertFalse(result.alert_supervisor)
        for field in ("entry_allowed", "live_authorization",
                      "stop_execution_certified", "continuous_protection_certified",
                      "emergency_close_executed", "mexc_orders_sent"):
            self.assertFalse(getattr(result, field), field)
        self.assertTrue(result.read_only)
        self.assertNotIn("900", str(result))
        self.assertNotIn(_ACCOUNT, str(result))

    def test_flat_inventory_is_not_stop_execution_certificate(self):
        result = self.check(evidence(flat=True))
        self.assertEqual(result.status, "WATCH_FLAT_INVENTORY_OBSERVED_NONAUTHORIZING")
        self.assertTrue(result.account_flat_observed)
        self.assertFalse(result.stop_execution_certified)

    def test_unprotected_position_alerts_even_before_threshold(self):
        ev = evidence(protected=False)
        result = self.check(ev)
        self.assertEqual(result.status, "WATCH_UNPROTECTED_EXPOSURE_NOW")
        self.assertTrue(result.alert_supervisor)
        self.assertFalse(result.emergency_close_executed)
        ev["deals"][0]["timestamp"] -= 10000
        ev["deals_bookend"][0]["timestamp"] -= 10000
        result = self.check(ev)
        self.assertEqual(result.status, "WATCH_UNPROTECTED_EXPOSURE_DEADLINE_BREACHED")
        self.assertTrue(result.alert_supervisor)

    def test_invalid_window_or_bookends_never_claim_protection(self):
        for field, bad in (
            ("start_ms", None),
            ("end_ms", True),
            ("end_ms", int(time.time()*1000)-10000),
            ("start_ms", int(time.time()*1000)+5000),
        ):
            ev = evidence()
            ev[field] = bad
            with self.subTest(field=field, bad=bad):
                self.assertFalse(self.check(ev).currently_active_stop_observed)
        ev = evidence()
        ev["after"]["positions"][0]["holdVol"] = "2"
        self.assertEqual(self.check(ev).status, "WATCH_BOOKEND_DRIFT_UNVERIFIED")
        ev = evidence()
        ev["deals_bookend"][0]["price"] = "101"
        self.assertEqual(self.check(ev).status, "WATCH_BOOKEND_DRIFT_UNVERIFIED")
        ev = evidence()
        ev["origin_bookend"]["state"] = 2
        self.assertEqual(self.check(ev).status, "WATCH_BOOKEND_DRIFT_UNVERIFIED")

    def test_fresh_wrong_account_missing_origin_or_unrelated_orders(self):
        ev = evidence()
        ev["account"] = "b"*64
        self.assertEqual(self.check(ev).status, "WATCH_ACCOUNT_OR_EVIDENCE_UNVERIFIED")
        ev = evidence()
        ev["origin"]["externalOid"] = "manual-entry"
        ev["origin_bookend"]["externalOid"] = "manual-entry"
        self.assertEqual(self.check(ev).status, "WATCH_OWNED_ENTRY_UNVERIFIED")
        ev = evidence()
        ev["before"]["counts"]["limitOrderCount"] = 1
        ev["before"]["counts"]["sumCount"] = 2
        ev["after"] = copy.deepcopy(ev["before"])
        self.assertEqual(self.check(ev).status, "WATCH_UNRELATED_ORDER_OR_COUNT_MISMATCH")
        ev = evidence()
        ev["before"]["positions"].append(dict(ev["before"]["positions"][0], positionId="901"))
        ev["after"] = copy.deepcopy(ev["before"])
        self.assertEqual(self.check(ev).status, "WATCH_POSITION_OWNERSHIP_OR_MODE_UNVERIFIED")

    def test_partial_false_or_malformed_entry_ledger_is_not_proof(self):
        ev = evidence()
        ev["deals"][0]["vol"] = "0.5"
        ev["deals_bookend"] = copy.deepcopy(ev["deals"])
        self.assertEqual(self.check(ev).status, "WATCH_ENTRY_FILL_VOLUME_UNVERIFIED")
        ev = evidence()
        ev["deals"].append(copy.deepcopy(ev["deals"][0]))
        ev["deals_bookend"] = copy.deepcopy(ev["deals"])
        self.assertEqual(self.check(ev).status, "WATCH_ENTRY_FILL_LEDGER_UNVERIFIED")
        ev = evidence()
        ev["deals"][0]["timestamp"] = True
        ev["deals_bookend"] = copy.deepcopy(ev["deals"])
        self.assertEqual(self.check(ev).status, "WATCH_ENTRY_FILL_LEDGER_UNVERIFIED")

    def test_stop_identity_native_semantics_and_volume_fail_closed(self):
        for key, bad, expected in (
            ("id", "98", "WATCH_STOP_IDENTITY_OR_UNIQUENESS_UNVERIFIED"),
            ("volType", 1, "WATCH_STOP_WHOLE_POSITION_MODE_UNVERIFIED"),
            ("profitLossVolType", "SEPARATE", "WATCH_STOP_WHOLE_POSITION_MODE_UNVERIFIED"),
            ("vol", "0.5", "WATCH_STOP_VOLUME_CONFLICT"),
            ("vol", True, "WATCH_STOP_VOLUME_CONFLICT"),
            ("stopLossType", 1, "WATCH_NATIVE_STOP_SEMANTICS_UNVERIFIED"),
            ("stopLossReverse", 1, "WATCH_NATIVE_STOP_SEMANTICS_UNVERIFIED"),
            ("orderId", 100, "WATCH_NATIVE_STOP_SEMANTICS_UNVERIFIED"),
            ("stopLossPrice", "111", "WATCH_NATIVE_STOP_SEMANTICS_UNVERIFIED"),
            ("state", 2, "WATCH_NATIVE_STOP_SEMANTICS_UNVERIFIED"),
        ):
            ev = evidence()
            ev["before"]["stops"][0][key] = bad
            ev["after"] = copy.deepcopy(ev["before"])
            with self.subTest(key=key, bad=bad):
                result = self.check(ev)
                self.assertEqual(result.status, expected)
                self.assertTrue(result.alert_supervisor)
                self.assertFalse(result.live_authorization)
        self.assertEqual(
            self.check(evidence(), scope(stop_id=None)).status,
            "WATCH_STOP_IDENTITY_OR_UNIQUENESS_UNVERIFIED",
        )

    def test_scope_malformed_cannot_approve_anything(self):
        for change in (
            {"account": "wrong"}, {"stop_id": "0"},
            {"origin_oid": "../other"}, {"maximum_stop_price": Decimal("NaN")},
            {"alert_after_ms": True}, {"alert_after_ms": 0},
            {"alert_after_ms": 15001},
        ):
            with self.subTest(change=change):
                r = self.check(evidence(), scope(**change))
                self.assertEqual(r.status, "WATCH_SCOPE_OR_CLOCK_INVALID")
                self.assertTrue(r.alert_supervisor)


class GetOnlyAdapterTests(unittest.TestCase):
    def test_double_gets_do_not_send_order_or_stop_mutations(self):
        ev = evidence()
        before = ev["before"]
        class GetClient:
            def __init__(self):
                self.calls = []
            async def _private_get(self, path, params=None):
                self.calls.append((path, params))
                if path == "/api/v1/private/position/open_positions":
                    return copy.deepcopy(before["positions"])
                if path == "/api/v1/private/stoporder/open_orders":
                    return copy.deepcopy(before["stops"])
                if path == "/api/v1/private/order/open_order_total_count":
                    return copy.deepcopy(before["counts"])
                if path.startswith("/api/v1/private/order/external/BTC_USDT/"):
                    return copy.deepcopy(ev["origin"])
                if path == "/api/v1/private/order/deal_details/100":
                    return copy.deepcopy(ev["deals"])
                raise AssertionError(path)
        client = GetClient()
        v = asyncio.run(observe_pilot_watch_get(ReadOnlyMexcProbe(client), scope()))
        self.assertEqual(v.status, "WATCH_NATIVE_STOP_CURRENTLY_OBSERVED_NONAUTHORIZING")
        self.assertEqual(len(client.calls), 10)
        self.assertTrue(all(path.startswith("/api/v1/private/")
                            for path, _ in client.calls))
        self.assertFalse(any("place" in path or "cancel" in path or "change" in path
                             for path, _ in client.calls))

    def test_failed_get_is_redacted_unknown_not_flat(self):
        class FailedGet:
            async def _private_get(self, path, params=None):
                raise RuntimeError("PRIVATE_RESPONSE_WITH_SECRET")
        v = asyncio.run(observe_pilot_watch_get(ReadOnlyMexcProbe(FailedGet()), scope()))
        self.assertEqual(v.status, "WATCH_SIGNED_GET_UNAVAILABLE")
        self.assertTrue(v.alert_supervisor)
        self.assertFalse(v.account_flat_observed)
        self.assertFalse(v.live_authorization)
        self.assertNotIn("PRIVATE_RESPONSE", repr(v))


if __name__ == "__main__":
    unittest.main()
