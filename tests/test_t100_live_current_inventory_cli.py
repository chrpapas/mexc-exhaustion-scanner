"""Offline tests for manual GET-only redacted account-completeness CLI."""
import asyncio
import unittest

from app.t100_live_current_inventory_cli import (
    _run,
    shareable_current_inventory_report,
)


class FakeReadClient:
    def __init__(self, *, protected=False, unprotected=False,
                 limit_order=False, mismatch=False, block=False):
        self.calls = []
        self.protected = protected
        self.unprotected = unprotected
        self.limit_order = limit_order
        self.mismatch = mismatch
        self.block = block

    async def _private_get(self, path, params=None):
        self.calls.append((path, params))
        if self.block:
            await asyncio.sleep(60)
        if path.endswith("/position/open_positions"):
            positions = []
            if self.protected or self.unprotected:
                positions = [{
                    "positionId": "SENSITIVE_POSITION_ID",
                    "symbol": "SIREN_USDT",
                    "positionType": 2,
                    "state": 1,
                    "holdVol": 5,
                    "im": 10,
                    "unRealizedPnl": 1,
                }]
            return [p for p in positions
                    if not params or p["symbol"] == params.get("symbol")]
        if path.endswith("/stoporder/open_orders"):
            stops = []
            if self.protected:
                stops = [{
                    "id": "SENSITIVE_STOP_ID",
                    "positionId": "SENSITIVE_POSITION_ID",
                    "symbol": "SIREN_USDT",
                    "positionType": 2,
                    "state": 1,
                    "isFinished": 0,
                    "stopLossPrice": 175,
                    "vol": 5,
                }]
            return [p for p in stops
                    if not params or p["symbol"] == params.get("symbol")]
        if path.endswith("/account/assets"):
            return [{
                "currency": "USDT",
                "positionMargin": 10 if self.protected or self.unprotected else 0,
                "unrealized": 1 if self.protected or self.unprotected else 0,
            }]
        if path.endswith("/order/open_order_total_count"):
            stop_count = 1 if self.protected else 0
            if self.mismatch:
                stop_count += 1
            return {
                "sumCount": stop_count + int(self.limit_order),
                "limitOrderCount": int(self.limit_order),
                "stopOrderCount": stop_count,
                "planOrderCount": 0,
                "trackOrderCount": 0,
            }
        raise AssertionError("UNEXPECTED_ENDPOINT")


class CurrentInventoryCliTests(unittest.IsolatedAsyncioTestCase):
    async def test_flat_inventory_reports_eligible_current_only(self):
        client = FakeReadClient()
        result = await _run(client=client)
        self.assertEqual(
            result["status"], "CURRENT_ACCOUNT_INVENTORY_COMPLETENESS_VERIFIED_NONATOMIC"
        )
        self.assertTrue(result["account_wide_completeness_verified"])
        self.assertFalse(result["exchange_atomicity_verified"])
        self.assertFalse(result["durable_nonflat_recovery_verified"])
        self.assertFalse(result["live_authorization"])
        self.assertEqual(result["after_positions"], 0)
        self.assertTrue(all(
            path.startswith("/api/v1/private/") for path, _ in client.calls
        ))

    async def test_nonflat_complete_inventory_does_not_claim_recovery(self):
        result = await _run(client=FakeReadClient(protected=True))
        self.assertTrue(result["account_wide_completeness_verified"])
        self.assertEqual(result["before_positions"], 1)
        self.assertEqual(result["before_current_tpsl"], 1)
        self.assertTrue(result["current_tpsl_count_matches"])
        self.assertTrue(result["position_margin_witness_verified"])
        self.assertTrue(result["symbol_scoped_redundancy_verified"])
        self.assertFalse(result["durable_nonflat_recovery_verified"])
        self.assertFalse(result["native_stop_protectiveness_independently_verified"])
        self.assertNotIn("SIREN_USDT", str(result))
        self.assertNotIn("SENSITIVE_POSITION_ID", str(result))
        self.assertNotIn("SENSITIVE_STOP_ID", str(result))
        self.assertNotIn("175", str(result))

    async def test_unprotected_nonflat_and_unrelated_limit_both_report(self):
        result = await _run(client=FakeReadClient(
            unprotected=True, limit_order=True,
        ))
        self.assertEqual(
            result["status"], "CURRENT_ACCOUNT_INVENTORY_COMPLETENESS_VERIFIED_NONATOMIC",
        )
        self.assertTrue(result["account_wide_completeness_verified"])
        self.assertTrue(result["position_margin_witness_verified"])
        self.assertEqual(result["after_positions"], 1)
        self.assertEqual(result["after_current_tpsl"], 0)
        self.assertEqual(result["reported_open_order_counts"]["limitOrderCount"], 1)
        self.assertEqual(result["account_reconciliation_status"],
                         "UNRELATED_INFLIGHT_ORDERS_PRESENT")
        self.assertEqual(result["highest_priority_blocker"],
                         "NONFLAT_WITHOUT_OBSERVED_CURRENT_TPSL")
        self.assertTrue(result["observed_nonflat_without_current_tpsl"])
        self.assertEqual(result["observed_unrelated_limit_orders"], 1)
        self.assertEqual(result["reconciliation_blockers"], [
            "NONFLAT_WITHOUT_OBSERVED_CURRENT_TPSL",
            "UNRELATED_INFLIGHT_ORDERS_PRESENT",
            "DURABLE_NONFLAT_RECOVERY_UNVERIFIED",
        ])
        self.assertFalse(result["account_reconciled"])
        self.assertFalse(result["entry_allowed"])
        self.assertFalse(result["live_authorization"])
        self.assertNotIn("SIREN_USDT", str(result))
        self.assertNotIn("SENSITIVE_POSITION_ID", str(result))

    async def test_stop_count_mismatch_blocks_account_completeness(self):
        result = await _run(
            client=FakeReadClient(protected=True, mismatch=True)
        )
        self.assertFalse(result["account_wide_completeness_verified"])
        self.assertFalse(result["current_tpsl_count_matches"])
        self.assertEqual(result["status"], "CURRENT_TPSL_COUNT_MISMATCH")
        self.assertFalse(result["live_authorization"])

    async def test_timeout_stays_redacted_and_blocked(self):
        result = await _run(
            client=FakeReadClient(block=True), timeout=0.001
        )
        self.assertEqual(
            result["status"],
            "CURRENT_INVENTORY_COMPLETENESS_DIAGNOSTIC_HALTED",
        )
        self.assertEqual(result["failure_type"], "TimeoutError")
        self.assertFalse(result["account_wide_completeness_verified"])
        self.assertFalse(result["live_authorization"])

    def test_allowlist_never_emits_raw_payload_values(self):
        result = shareable_current_inventory_report({
            "status": "CURRENT_INVENTORY_PROBE_HALTED",
            "failure_type": "VenueReadHalted",
            "symbol": "DO_NOT_LEAK_SYMBOL",
            "api_secret": "DO_NOT_LEAK_SECRET",
            "open_order_counts": {
                "stopOrderCount": 1,
                "untrusted": "DO_NOT_LEAK_PAYLOAD",
            },
            "before_positions": 1,
            "after_positions": 1,
        })
        for secret in (
            "DO_NOT_LEAK_SYMBOL", "DO_NOT_LEAK_SECRET",
            "DO_NOT_LEAK_PAYLOAD"
        ):
            self.assertNotIn(secret, str(result))
        self.assertEqual(result["reported_open_order_counts"]["stopOrderCount"], 1)
        self.assertIsNone(result["reported_open_order_counts"]["sumCount"])
        self.assertFalse(result["live_authorization"])


if __name__ == "__main__":
    unittest.main()
