"""Offline, mocked PUBLIC GET-only V40 capacity diagnostic tests."""
from __future__ import annotations

import copy
import unittest

from app.t100_live_public_v40_capacity_cli import inspect, run, shareable
from app.t100_live_public_v40_capacity import PublicV40CapacityReview
from tests.test_t100_live_public_v40_capacity import siren


class Response:
    def __init__(self, raw):
        self.raw = raw
    def raise_for_status(self):
        pass
    def json(self):
        return {"success": True, "code": 0, "data": copy.deepcopy(self.raw)}


class Public:
    def __init__(self, first, second=None, fail=False):
        self.payloads = [first, copy.deepcopy(first) if second is None else second]
        self.calls = []
        self.fail = fail
    async def get(self, path, *, params):
        self.calls.append((path, dict(params)))
        if self.fail:
            raise OSError("SECRET_IN_EXCEPTION")
        return Response(self.payloads[len(self.calls) - 1])


class CapacityCliTests(unittest.IsolatedAsyncioTestCase):
    async def test_oversized_slot_returns_lots_and_public_limits_without_auth(self):
        public = Public(siren())
        result = await inspect(
            public, "SIREN_USDT", target_notional_usdt="5625",
            entry_reference_price="0.02298", leverage=1,
        )
        self.assertEqual(result["status"],
                         "V40_CAPACITY_PUBLISHED_POSITION_TIER_EXCEEDED_NONAUTHORIZING")
        self.assertEqual(result["required_lots"], "24477")
        self.assertEqual(result["public_max_single_order_lots"], "3600")
        self.assertEqual(result["public_max_position_tier_lots"], "3600")
        self.assertEqual(len(public.calls), 2)
        self.assertEqual(public.calls[0][0], "/api/v1/contract/detail/country")
        self.assertEqual(public.calls[0][1], {"symbol": "SIREN_USDT"})
        self.assertFalse(result["private_current_cap_verified"])
        self.assertFalse(result["depth_and_execution_capacity_verified"])
        self.assertFalse(result["live_authorization"])

    async def test_mocked_public_drift_and_error_fail_closed(self):
        first = siren()
        second = copy.deepcopy(first)
        second["maxVol"] = 1
        p = Public(first, second)
        r = await inspect(
            p, "SIREN_USDT", target_notional_usdt="500",
            entry_reference_price="0.02298", leverage=1,
        )
        self.assertEqual(r["status"], "V40_CAPACITY_PUBLIC_METADATA_DRIFT")
        self.assertIsNone(r["required_lots"])
        p = Public(first, fail=True)
        r = await inspect(
            p, "SIREN_USDT", target_notional_usdt="500",
            entry_reference_price="0.02298", leverage=1,
        )
        self.assertEqual(r["status"], "V40_CAPACITY_PUBLIC_GET_FAILED")
        self.assertNotIn("SECRET_IN_EXCEPTION", str(r))

    async def test_public_get_guard_no_account_credential_or_trading(self):
        public = Public(siren())
        blocked = await run(
            "SIREN_USDT", target_notional_usdt="500",
            entry_reference_price="0.02298", public=public,
            environ={
                "T100_EXECUTION_MODE": "live",
                "T100_LIVE_ORDER_API_ENABLED": "false",
                "T100_LIVE_KILL_SWITCH": "true",
            },
        )
        self.assertEqual(blocked["status"], "V40_CAPACITY_PAPER_KILL_REQUIRED")
        self.assertEqual(public.calls, [])
        allowed = await run(
            "SIREN_USDT", target_notional_usdt="500",
            entry_reference_price="0.02298", public=public,
            environ={
                "T100_EXECUTION_MODE": "paper",
                "T100_LIVE_ORDER_API_ENABLED": "false",
                "T100_LIVE_KILL_SWITCH": "true",
            },
        )
        self.assertEqual(
            allowed["status"], "V40_CAPACITY_PUBLISHED_SINGLE_ORDER_FITS_NONAUTHORIZING",
        )
        self.assertEqual(len(public.calls), 2)

    async def test_invalid_symbol_never_reaches_public(self):
        public = Public(siren())
        r = await inspect(
            public, "../../../private", target_notional_usdt="100",
            entry_reference_price="0.1", leverage=1,
        )
        self.assertEqual(r["status"], "V40_CAPACITY_SYMBOL_INVALID")
        self.assertEqual(public.calls, [])

    def test_status_allowlist_has_no_raw_exchange_payload(self):
        r = shareable(PublicV40CapacityReview("V40_CAPACITY_INVALID_OR_INCOMPLETE"))
        self.assertFalse(r["entry_allowed"])
        self.assertFalse(r["mexc_orders_sent"])
        self.assertFalse(r["private_current_cap_verified"])
        self.assertNotIn("SIREN_USDT", str(r))


if __name__ == "__main__":
    unittest.main()
