"""Mocked standalone GET-only pilot service integration; no live venue calls."""
from __future__ import annotations

import asyncio
import hashlib
import hmac
import os
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

import httpx

from app.t100_pilot_watchdog_service import (
    ServiceHalted, SignedGetOnlyMexc, build_config, run_service,
)


class ServiceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.journal = Path(self.temp.name) / "pilot-scope.sqlite"
        key = "scoped-read-key-not-an-exchange-key"
        self.env = {
            "T100_EXECUTION_MODE": "paper",
            "T100_LIVE_ORDER_API_ENABLED": "false",
            "T100_LIVE_KILL_SWITCH": "true",
            "T100_PILOT_READONLY_API_KEY": key,
            "T100_PILOT_READONLY_API_SECRET": "testing-secret-only",
            "T100_PILOT_EXPECTED_API_KEY_SHA256": hashlib.sha256(key.encode()).hexdigest(),
            "T100_PILOT_ORIGIN_OID": "pilot-owned-entry",
            "T100_PILOT_STOP_ID": "99",
            "T100_PILOT_MAX_STOP_PRICE": "110",
            "T100_PILOT_WATCHDOG_JOURNAL": str(self.journal),
            "T100_PILOT_DISCORD_WEBHOOK_URL": "https://discord.com/api/webhooks/123/mock",
        }

    def tearDown(self):
        self.temp.cleanup()

    def test_valid_explicit_config_and_journal_is_scope_bound(self):
        first = build_config(self.env, cycles=1)
        self.assertEqual(first[4].origin_oid, "pilot-owned-entry")
        self.assertEqual(first[4].stop_id, "99")
        self.assertEqual(first[4].account, self.env["T100_PILOT_EXPECTED_API_KEY_SHA256"])
        changed = dict(self.env, T100_PILOT_ORIGIN_OID="another")
        self.assertNotEqual(first[5], build_config(changed, cycles=1)[5])

    def test_fail_closed_for_missing_key_pin_order_execution_or_webhook(self):
        cases = [
            ("T100_EXECUTION_MODE", "real", "PILOT_PAPER_KILL_REQUIRED"),
            ("T100_LIVE_ORDER_API_ENABLED", "true", "PILOT_PAPER_KILL_REQUIRED"),
            ("T100_LIVE_KILL_SWITCH", "false", "PILOT_PAPER_KILL_REQUIRED"),
            ("T100_PILOT_EXPECTED_API_KEY_SHA256", "0" * 64, "PILOT_DEDICATED_KEY_PIN_UNVERIFIED"),
            ("T100_PILOT_READONLY_API_SECRET", "", "PILOT_DEDICATED_KEY_PIN_UNVERIFIED"),
            ("T100_PILOT_ORIGIN_OID", "bad/slash", "PILOT_SCOPE_INVALID"),
            ("T100_PILOT_STOP_ID", "0", "PILOT_SCOPE_INVALID"),
            ("T100_PILOT_MAX_STOP_PRICE", "NaN", "PILOT_SCOPE_INVALID"),
            ("T100_PILOT_DISCORD_WEBHOOK_URL", "https://example.com", "PILOT_DISCORD_WEBHOOK_REQUIRED"),
            ("T100_PILOT_WATCHDOG_JOURNAL", "relative.db", "PILOT_ABSOLUTE_JOURNAL_REQUIRED"),
        ]
        for key, val, expected in cases:
            with self.subTest(key=key, val=val):
                e = dict(self.env, **{key: val})
                with self.assertRaisesRegex(ServiceHalted, expected):
                    build_config(e, cycles=1)
        with self.assertRaisesRegex(ServiceHalted, "PILOT_INVALID_CYCLES"):
            build_config(self.env, cycles=0)

    def test_local_sentinel_scope_cli_uses_no_network_or_journal(self):
        pinned = build_config(self.env, cycles=1)[5]
        result = subprocess.run(
            [sys.executable, "-m", "app.t100_pilot_watchdog_service",
             "--print-sentinel-scope"],
            env={**os.environ, **self.env},
            capture_output=True, text=True, timeout=8,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("PILOT_SENTINEL_SCOPE_SHA256=" + pinned, result.stdout)
        self.assertNotIn(self.env["T100_PILOT_READONLY_API_KEY"], result.stdout)
        self.assertNotIn(self.env["T100_PILOT_READONLY_API_SECRET"], result.stdout)
        self.assertIn("NO NETWORK OR JOURNAL OPERATIONS", result.stdout)
        self.assertFalse(self.journal.exists())

    def test_signed_get_uses_only_exact_allowlisted_paths_and_no_post(self):
        async def scenario():
            client = SignedGetOnlyMexc("test-key", "test-secret")
            try:
                request = httpx.Request("GET", "https://api.mexc.com/api/v1/private/position/open_positions")
                response = httpx.Response(
                    200, json={"success": True, "code": 0, "data": []},
                    request=request,
                )
                client._http.get = AsyncMock(return_value=response)
                got = await client._private_get("/api/v1/private/position/open_positions")
                self.assertEqual(got, [])
                self.assertFalse(hasattr(client, "_private_post"))
                headers = client._http.get.call_args.kwargs["headers"]
                signature = hmac.new(
                    b"test-secret", ("test-key" + headers["Request-Time"]).encode(),
                    hashlib.sha256,
                ).hexdigest()
                self.assertEqual(headers["Signature"], signature)
                with self.assertRaisesRegex(ServiceHalted, "ENDPOINT_NOT_ALLOWLISTED"):
                    await client._private_get("/api/v1/private/order/create")
                with self.assertRaisesRegex(ServiceHalted, "QUERY_NOT_ALLOWLISTED"):
                    await client._private_get("/api/v1/private/position/open_positions", {"symbol": "BTC_USDT"})
                self.assertEqual(client._http.get.await_count, 1)
            finally:
                await client.close()
        asyncio.run(scenario())

    def test_get_application_failure_redacts_private_message(self):
        async def scenario():
            client = SignedGetOnlyMexc("test-key", "test-secret")
            try:
                request = httpx.Request("GET", "https://api.mexc.com/api/v1/private/position/open_positions")
                response = httpx.Response(
                    200, json={"success": False, "code": 602, "message": "SECRET_ACCOUNT"},
                    request=request,
                )
                client._http.get = AsyncMock(return_value=response)
                with self.assertRaisesRegex(ServiceHalted, "PILOT_GET_REJECTED") as ctx:
                    await client._private_get("/api/v1/private/position/open_positions")
                self.assertNotIn("SECRET_ACCOUNT", str(ctx.exception))
            finally:
                await client.close()
        asyncio.run(scenario())

    def test_end_to_end_injected_get_and_notification_only(self):
        import time
        events = []
        paths = []
        # Signed two-bookend audit MUST see one immutable execution timestamp.
        # Recomputing now() on every mocked fill GET creates false venue drift.
        fixed_fill_ms = int(time.time() * 1000) - 1000
        class FakeMexc:
            def __init__(self, *_):
                pass
            async def _private_get(self, path, params=None):
                paths.append(path)
                if path == "/api/v1/private/position/open_positions":
                    return [{
                        "positionId": "900", "symbol": "BTC_USDT",
                        "holdVol": "1", "positionType": 2,
                        "openType": 1, "leverage": 1,
                    }]
                if path == "/api/v1/private/stoporder/open_orders":
                    return []   # intentionally UNPROTECTED
                if path == "/api/v1/private/order/open_order_total_count":
                    return {
                        "sumCount": 0, "stopOrderCount": 0,
                        "limitOrderCount": 0, "planOrderCount": 0, "trackOrderCount": 0,
                    }
                if path == "/api/v1/private/order/external/BTC_USDT/pilot-owned-entry":
                    return {
                        "orderId": "100", "positionId": "900",
                        "symbol": "BTC_USDT", "externalOid": "pilot-owned-entry",
                        "side": 3, "state": 3, "dealVol": "1",
                    }
                if path == "/api/v1/private/order/deal_details/100":
                    return [{
                        "id": "201", "orderId": "100", "symbol": "BTC_USDT",
                        "side": 3, "vol": "1", "price": "100",
                        "timestamp": fixed_fill_ms,
                    }]
                raise AssertionError("unexpected GET path")
            async def close(self):
                return None

        class FakeNotifier:
            def __init__(self, *_args, **_kwargs):
                pass
            async def send(self, notice, *, force=False):
                events.append((notice.severity, notice.description))
                return True
            async def close(self):
                pass

        async def scenario():
            with (
                patch("app.t100_pilot_watchdog_service.SignedGetOnlyMexc", FakeMexc),
                patch("app.t100_pilot_watchdog_service.T100LiveOpsNotifier", FakeNotifier),
            ):
                result = await run_service(environ=self.env, cycles=1)
                self.assertEqual(result.status, "WATCH_UNPROTECTED_EXPOSURE_NOW")
                self.assertTrue(result.alarm_latched)
                self.assertFalse(result.live_authorization)
                self.assertFalse(result.mexc_orders_sent)
        asyncio.run(scenario())
        self.assertEqual(len(paths), 10)
        self.assertTrue(all(x.startswith("/api/v1/private/") for x in paths))
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0], ("critical", "WATCH_UNPROTECTED_EXPOSURE_NOW"))
        with sqlite3.connect(self.journal) as db:
            self.assertEqual(db.execute("SELECT alarm_latched FROM state").fetchone()[0], 1)
            self.assertEqual(db.execute("SELECT COUNT(*) FROM outbox WHERE delivered_ms IS NULL").fetchone()[0], 0)


if __name__ == "__main__":
    unittest.main()
