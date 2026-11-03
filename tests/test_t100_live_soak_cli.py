"""Offline tests for the operator-local GET-only soak CLI helper."""
import os
import unittest
from unittest.mock import patch

from app.t100_live_soak import DiscoverySoakConfig
from app.t100_live_soak_cli import run_read_only_soak


class FakeClient:
    def __init__(self):
        self.calls = []
        self.position = [{
            "symbol": "SECRET_USDT",
            "positionId": 123456789,
            "positionType": 2,
            "holdVol": 777,
            "state": 1,
        }]
        self.stops = [{
            "id": 987654321,
            "positionId": "123456789",
            "symbol": "SECRET_USDT",
            "state": 1,
            "stopLossPrice": 543.21,
            "vol": 777,
        }]

    async def _private_get(self, path, params=None):
        self.calls.append((path, params))
        if path == "/api/v1/private/position/open_positions":
            return list(self.position)
        if path == "/api/v1/private/stoporder/open_orders":
            return list(self.stops)
        raise AssertionError("unexpected endpoint")


class SoakCliTests(unittest.IsolatedAsyncioTestCase):
    async def test_injected_client_stays_get_only_and_redacted(self):
        client = FakeClient()
        with patch.dict(os.environ, {"T100_EXECUTION_MODE": "paper"}, clear=False):
            result = await run_read_only_soak(
                config=DiscoverySoakConfig(
                    samples=2,
                    interval_seconds=0,
                    max_runtime_seconds=5,
                ),
                client=client,
            )
        self.assertEqual(result["status"], "COMPLETED_READ_ONLY_DISCOVERY")
        self.assertEqual(result["completed_samples"], 2)
        self.assertFalse(result["live_order_submission_possible"])
        self.assertFalse(result["entry_allowed"])
        self.assertFalse(result["live_authorization"])
        self.assertEqual(len(client.calls), 8)
        self.assertTrue(all(call[0].startswith("/api/v1/private/") for call in client.calls))
        text = str(result)
        for secret in (
            "SECRET_USDT", "123456789", "987654321", "543.21", "777",
        ):
            self.assertNotIn(secret, text)

    async def test_nonpaper_mode_refused_without_private_reads(self):
        client = FakeClient()
        with patch.dict(
            os.environ,
            {"T100_EXECUTION_MODE": "live"},
            clear=False,
        ):
            result = await run_read_only_soak(
                config=DiscoverySoakConfig(
                    samples=2,
                    interval_seconds=0,
                    max_runtime_seconds=5,
                ),
                client=client,
            )
        self.assertEqual(result["status"], "REFUSED_NON_PAPER_EXECUTION_MODE")
        self.assertTrue(result["kill_tripped"])
        self.assertEqual(client.calls, [])

    async def test_missing_credentials_blocks_when_client_not_injected(self):
        with patch.dict(
            os.environ,
            {
                "T100_EXECUTION_MODE": "paper",
                "MEXC_API_KEY": "",
                "MEXC_API_SECRET": "",
            },
            clear=False,
        ):
            result = await run_read_only_soak(
                config=DiscoverySoakConfig(
                    samples=2,
                    interval_seconds=0,
                    max_runtime_seconds=5,
                )
            )
        self.assertEqual(
            result["status"], "MISSING_CREDENTIALS_NO_PRIVATE_READS")
        self.assertTrue(result["kill_tripped"])
