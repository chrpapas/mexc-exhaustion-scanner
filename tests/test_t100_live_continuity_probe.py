"""Offline tests for bounded REST-WS-REST continuity evidence."""
import os
import unittest
from unittest.mock import patch

from app.t100_live_continuity_probe import run_continuity_probe
from app.t100_live_private_ws import PrivateWsReport


class FakeClient:
    def __init__(self):
        self.calls = []
        self.position_reads = [
            [{
                "symbol": "SIREN_USDT", "positionId": 10,
                "positionType": 2, "state": 1, "holdVol": 5,
                "holdAvgPrice": 100, "unRealizedPnl": 1.0,
            }],
            [{
                "symbol": "SIREN_USDT", "positionId": 10,
                "positionType": 2, "state": 1, "holdVol": 5,
                "holdAvgPrice": 100, "unRealizedPnl": 2.0,
            }],
        ]
        self.stop_reads = [[], []]

    async def _private_get(self, path, params=None):
        self.calls.append((path, params))
        if path == "/api/v1/private/position/open_positions":
            return self.position_reads.pop(0)
        if path == "/api/v1/private/stoporder/open_orders":
            return self.stop_reads.pop(0)
        raise AssertionError("unexpected endpoint")


def ws_report(events=0, unexpected=()):
    return PrivateWsReport(
        login_verified=True,
        filter_sent=True,
        pongs_seen=1,
        private_events_seen=events,
        position_events_seen=events,
        stop_plan_events_seen=0,
        fill_events_seen=0,
        unexpected_private_channels=unexpected,
        channel_schema={},
        elapsed_ms=1000,
        status="COMPLETED_PRIVATE_WS_OBSERVATION",
    )


class ContinuityProbeTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.env = patch.dict(os.environ, {
            "T100_EXECUTION_MODE": "paper",
            "MEXC_API_KEY": "KEY",
            "MEXC_API_SECRET": "SECRET",
        }, clear=False)
        self.env.start()

    async def asyncTearDown(self):
        self.env.stop()

    async def test_volatile_raw_drift_keeps_critical_continuity_stable(self):
        async def fake_ws(**kwargs):
            return ws_report()
        result = await run_continuity_probe(
            duration_seconds=1,
            client=FakeClient(),
            ws_observer=fake_ws,
        )
        self.assertEqual(result["status"], "REST_WS_REST_CONTINUITY_STABLE")
        self.assertTrue(result["critical_rest_inventory_equal"])
        self.assertFalse(result["full_raw_positions_equal"])
        self.assertEqual(
            result["changed_position_field_names"], ["unRealizedPnl"])
        self.assertFalse(result["entry_allowed"])
        self.assertFalse(result["live_authorization"])
        self.assertFalse(result["account_wide_completeness_verified"])
        self.assertNotIn("SIREN_USDT", str(result))
        self.assertNotIn("100", str(result))

    async def test_critical_position_change_is_reported(self):
        client = FakeClient()
        client.position_reads[1][0]["holdVol"] = 4
        async def fake_ws(**kwargs):
            return ws_report(events=1)
        result = await run_continuity_probe(
            duration_seconds=1,
            client=client,
            ws_observer=fake_ws,
        )
        self.assertEqual(
            result["status"],
            "CRITICAL_REST_INVENTORY_CHANGED_DURING_WS_WINDOW",
        )
        self.assertFalse(result["critical_rest_inventory_equal"])

    async def test_private_events_are_preserved_without_authorization(self):
        async def fake_ws(**kwargs):
            return ws_report(events=1)
        result = await run_continuity_probe(
            duration_seconds=1,
            client=FakeClient(),
            ws_observer=fake_ws,
        )
        self.assertEqual(
            result["status"], "STABLE_REST_WITH_PRIVATE_WS_EVENTS_OBSERVED")
        self.assertEqual(result["ws_observation"]["private_events_seen"], 1)
        self.assertFalse(result["live_authorization"])

    async def test_unexpected_ws_channel_changes_status(self):
        async def fake_ws(**kwargs):
            return ws_report(unexpected=("push.personal.asset",))
        result = await run_continuity_probe(
            duration_seconds=1,
            client=FakeClient(),
            ws_observer=fake_ws,
        )
        self.assertEqual(
            result["status"],
            "STABLE_REST_WITH_UNEXPECTED_PRIVATE_WS_CHANNELS",
        )

    async def test_nonpaper_mode_refused_before_reads(self):
        client = FakeClient()
        with patch.dict(
            os.environ, {"T100_EXECUTION_MODE": "live"}, clear=False
        ):
            result = await run_continuity_probe(
                duration_seconds=1,
                client=client,
                ws_observer=lambda **kwargs: None,
            )
        self.assertEqual(
            result["status"], "REFUSED_NON_PAPER_EXECUTION_MODE")
        self.assertEqual(client.calls, [])

    async def test_missing_credentials_blocks(self):
        with patch.dict(os.environ, {
            "T100_EXECUTION_MODE": "paper",
            "MEXC_API_KEY": "",
            "MEXC_API_SECRET": "",
        }, clear=False):
            result = await run_continuity_probe(
                duration_seconds=1,
                client=FakeClient(),
            )
        self.assertEqual(
            result["status"], "MISSING_CREDENTIALS_NO_PRIVATE_READS")
