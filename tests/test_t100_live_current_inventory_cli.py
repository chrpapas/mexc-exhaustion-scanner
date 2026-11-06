"""Offline tests for manual GET-only redacted account-completeness CLI."""
import asyncio
import io
import json
import os
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from app.t100_live_current_inventory_cli import (
    PilotKeychainUnavailable,
    _main,
    _run,
    read_pilot_keychain_credentials,
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

    def test_pilot_keychain_uses_fixed_service_and_never_substitutes_monitoring(self):
        secret_pair = (
            SimpleNamespace(returncode=0, stdout="PILOT_KEY_PRIVATE\n"),
            SimpleNamespace(returncode=0, stdout="PILOT_SECRET_PRIVATE\n"),
        )
        with (
            patch("app.t100_live_current_inventory_cli.platform.system",
                  return_value="Darwin"),
            patch("app.t100_live_current_inventory_cli.subprocess.run",
                  side_effect=secret_pair) as run,
        ):
            self.assertEqual(
                read_pilot_keychain_credentials(),
                ("PILOT_KEY_PRIVATE", "PILOT_SECRET_PRIVATE"),
            )
        self.assertEqual(run.call_count, 2)
        for call in run.call_args_list:
            args = call.args[0]
            self.assertEqual(args[0], "/usr/bin/security")
            self.assertIn("t100225-mexc-pilot-subaccount", args)
            self.assertNotIn("t100-render-monitoring-db-roles-v1", args)
        self.assertEqual(
            [call.args[0][-2] for call in run.call_args_list],
            ["MEXC_API_KEY", "MEXC_API_SECRET"],
        )

    def test_missing_pilot_credential_fails_closed(self):
        with (
            patch("app.t100_live_current_inventory_cli.platform.system",
                  return_value="Darwin"),
            patch("app.t100_live_current_inventory_cli.subprocess.run",
                  return_value=SimpleNamespace(returncode=44, stdout="")),
        ):
            with self.assertRaisesRegex(
                PilotKeychainUnavailable,
                "T100_PILOT_KEYCHAIN_CREDENTIAL_MISSING",
            ):
                read_pilot_keychain_credentials()

    async def test_main_keychain_only_scope_and_no_secret_output(self):
        class Client:
            def __init__(self, host, *, api_key, api_secret):
                self.host, self.api_key, self.api_secret = host, api_key, api_secret
                self.closed = False

            async def close(self):
                self.closed = True

        client_objects = []
        def create_client(host, *, api_key, api_secret):
            c = Client(host, api_key=api_key, api_secret=api_secret)
            client_objects.append(c)
            return c

        output = io.StringIO()
        with (
            patch.dict(os.environ, {
                "T100_EXECUTION_MODE": "paper",
                "T100_LIVE_ORDER_API_ENABLED": "false",
                "T100_LIVE_KILL_SWITCH": "true",
            }, clear=True),
            patch("app.t100_live_current_inventory_cli.read_pilot_keychain_credentials",
                  return_value=("PILOT_KEY_PRIVATE", "PILOT_SECRET_PRIVATE")),
            patch.dict("sys.modules", {
                "app.mexc_trade": SimpleNamespace(MexcTradeClient=create_client),
            }),
            patch("app.t100_live_current_inventory_cli._run",
                  new=AsyncMock(return_value={
                      "status": "CURRENT_ACCOUNT_INVENTORY_COMPLETENESS_VERIFIED_NONATOMIC",
                      "read_only": True,
                      "live_authorization": False,
                  })),
            patch("sys.stdout", output),
        ):
            await _main(mac_pilot_keychain=True)
        self.assertEqual(len(client_objects), 1)
        self.assertTrue(client_objects[0].closed)
        self.assertEqual(client_objects[0].host, "https://api.mexc.com")
        value = output.getvalue()
        report = json.loads(value.split("\nPRIVATE GET ONLY.", 1)[0])
        self.assertEqual(report["credential_scope"], "T100_V5_PILOT_SUBACCOUNT")
        self.assertFalse(report["render_shadow_account_binding_verified"])
        self.assertFalse(report["live_authorization"])
        self.assertNotIn("PILOT_KEY_PRIVATE", value)
        self.assertNotIn("PILOT_SECRET_PRIVATE", value)

    async def test_no_keychain_read_when_live_switch_enabled(self):
        output = io.StringIO()
        with (
            patch.dict(os.environ, {
                "T100_EXECUTION_MODE": "paper",
                "T100_LIVE_ORDER_API_ENABLED": "true",
                "T100_LIVE_KILL_SWITCH": "true",
            }, clear=True),
            patch("app.t100_live_current_inventory_cli.read_pilot_keychain_credentials") as lookup,
            patch("sys.stdout", output),
        ):
            with self.assertRaises(SystemExit):
                await _main(mac_pilot_keychain=True)
        lookup.assert_not_called()
        self.assertIn("CURRENT_INVENTORY_READ_ONLY_MODE_REQUIRED", output.getvalue())

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
