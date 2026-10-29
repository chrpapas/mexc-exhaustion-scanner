"""Offline tests for the operator-local private WS probe CLI helper."""
import os
import unittest
from unittest.mock import patch

from app.t100_live_private_ws import PrivateWsReport
from app.t100_live_ws_probe import run_ws_probe


class WsProbeTests(unittest.IsolatedAsyncioTestCase):
    async def test_success_is_redacted_and_non_authorizing(self):
        report = PrivateWsReport(
            login_verified=True,
            filter_sent=True,
            pongs_seen=2,
            private_events_seen=1,
            position_events_seen=1,
            stop_plan_events_seen=0,
            fill_events_seen=0,
            unexpected_private_channels=(),
            channel_schema={
                "push.personal.position": {
                    "events_seen": 1,
                    "field_types": {"positionId": ["integer"]},
                }
            },
            elapsed_ms=1000,
            status="COMPLETED_PRIVATE_WS_OBSERVATION",
        )

        async def fake_observe(**kwargs):
            self.assertEqual(kwargs["api_key"], "VERY_SECRET_KEY")
            self.assertEqual(kwargs["api_secret"], "VERY_SECRET_SECRET")
            return report

        with patch.dict(os.environ, {
            "T100_EXECUTION_MODE": "paper",
            "MEXC_API_KEY": "VERY_SECRET_KEY",
            "MEXC_API_SECRET": "VERY_SECRET_SECRET",
        }, clear=False), patch(
            "app.t100_live_ws_probe.observe_private_ws", fake_observe
        ):
            result = await run_ws_probe(duration_seconds=1)

        self.assertTrue(result["login_verified"])
        self.assertFalse(result["entry_allowed"])
        self.assertFalse(result["live_authorization"])
        self.assertFalse(result["rest_snapshot_completeness_verified"])
        text = str(result)
        self.assertNotIn("VERY_SECRET_KEY", text)
        self.assertNotIn("VERY_SECRET_SECRET", text)

    async def test_nonpaper_mode_refused_before_observer(self):
        called = False

        async def fake_observe(**kwargs):
            nonlocal called
            called = True
            raise AssertionError()

        with patch.dict(os.environ, {
            "T100_EXECUTION_MODE": "live",
            "MEXC_API_KEY": "x",
            "MEXC_API_SECRET": "y",
        }, clear=False), patch(
            "app.t100_live_ws_probe.observe_private_ws", fake_observe
        ):
            result = await run_ws_probe(duration_seconds=1)
        self.assertFalse(called)
        self.assertEqual(
            result["status"], "REFUSED_NON_PAPER_EXECUTION_MODE")

    async def test_missing_credentials_blocks(self):
        with patch.dict(os.environ, {
            "T100_EXECUTION_MODE": "paper",
            "MEXC_API_KEY": "",
            "MEXC_API_SECRET": "",
        }, clear=False):
            result = await run_ws_probe(duration_seconds=1)
        self.assertEqual(
            result["status"], "MISSING_CREDENTIALS_NO_WS_LOGIN")
        self.assertFalse(result["login_verified"])
