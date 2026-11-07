"""Mocked Discord-only sentinel CLI wiring; no exchange credentials or POST."""
from __future__ import annotations

import asyncio
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from app.t100_pilot_watchdog_sentinel_cli import (
    SentinelServiceHalted, parse_config, run_sentinel,
)

SCOPE = "f" * 64


class LivenessCliTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.watched = Path(self.temp.name) / "missing-watchdog.sqlite"
        self.sentinel = Path(self.temp.name) / "sentinel-outbox.sqlite"
        self.events = []
        self.env = {
            "T100_EXECUTION_MODE": "paper",
            "T100_LIVE_ORDER_API_ENABLED": "false",
            "T100_LIVE_KILL_SWITCH": "true",
            "T100_PILOT_SENTINEL_EXPECTED_SCOPE_SHA256": SCOPE,
            "T100_PILOT_SENTINEL_WATCHDOG_JOURNAL": str(self.watched),
            "T100_PILOT_SENTINEL_ALERT_JOURNAL": str(self.sentinel),
            "T100_PILOT_SENTINEL_DISCORD_WEBHOOK_URL":
                "https://discord.com/api/webhooks/12345678/mock-token",
        }

    def tearDown(self):
        self.temp.cleanup()

    def test_no_mexc_credentials_are_required_by_liveness_cli(self):
        watched, outbox, scope, url = parse_config(self.env, cycles=1)
        self.assertEqual(watched, self.watched)
        self.assertEqual(outbox, self.sentinel)
        self.assertEqual(scope, SCOPE)
        self.assertTrue(url.startswith("https://discord.com/"))
        self.assertNotIn("MEXC_API_KEY", self.env)

    def test_rejects_live_controls_wrong_path_shared_webhook_and_scope(self):
        cases = [
            ("T100_EXECUTION_MODE", "live", "SENTINEL_PAPER_KILL_REQUIRED"),
            ("T100_LIVE_ORDER_API_ENABLED", "true", "SENTINEL_PAPER_KILL_REQUIRED"),
            ("T100_LIVE_KILL_SWITCH", "false", "SENTINEL_PAPER_KILL_REQUIRED"),
            ("T100_PILOT_SENTINEL_EXPECTED_SCOPE_SHA256", "bad", "SENTINEL_EXPECTED_SCOPE_MISSING"),
            ("T100_PILOT_SENTINEL_WATCHDOG_JOURNAL", "relative.sqlite", "SENTINEL_SEPARATE_ABSOLUTE_JOURNALS"),
            ("T100_PILOT_SENTINEL_ALERT_JOURNAL", str(self.watched), "SENTINEL_SEPARATE_ABSOLUTE_JOURNALS"),
            ("T100_PILOT_SENTINEL_DISCORD_WEBHOOK_URL", "https://example.com/secret", "SENTINEL_INDEPENDENT_DISCORD_REQUIRED"),
        ]
        for key, val, expected in cases:
            with self.subTest(key=key):
                with self.assertRaisesRegex(SentinelServiceHalted, expected):
                    parse_config(dict(self.env, **{key: val}), cycles=1)
        env = dict(self.env, DISCORD_TRADER_EVENTS_WEBHOOK_URL=self.env["T100_PILOT_SENTINEL_DISCORD_WEBHOOK_URL"])
        with self.assertRaisesRegex(SentinelServiceHalted, "SENTINEL_INDEPENDENT_DISCORD_REQUIRED"):
            parse_config(env, cycles=1)

    def test_mock_discord_alert_delivery_while_watched_process_is_missing(self):
        events = self.events
        class FakeNotifier:
            def __init__(self, *_args, **_kwargs):
                pass
            async def send(self, notice, *, force=False):
                events.append((notice.description, notice.severity))
                return True
            async def close(self):
                pass
        async def scenario():
            with patch("app.t100_live_ops_notifier.T100LiveOpsNotifier", FakeNotifier):
                result = await run_sentinel(environ=self.env, cycles=1)
                self.assertEqual(result.status, "WATCH_HEARTBEAT_JOURNAL_UNAVAILABLE")
                self.assertTrue(result.alarm_latched)
                self.assertFalse(result.mexc_orders_sent)
                self.assertEqual(result.notifications_pending, 0)
        asyncio.run(scenario())
        self.assertEqual(self.events, [("WATCH_HEARTBEAT_JOURNAL_UNAVAILABLE", "critical")])
        self.assertFalse(self.watched.exists())
        self.assertTrue(self.sentinel.exists())
        with sqlite3.connect(self.sentinel) as db:
            self.assertEqual(db.execute("SELECT alarm_latched FROM sentinel_state").fetchone()[0], 1)

    def test_repeat_restart_does_not_duplicate_delivered_discord_alert(self):
        events = self.events
        class FakeNotifier:
            def __init__(self, *_args, **_kwargs):
                pass
            async def send(self, notice, *, force=False):
                events.append(notice.description)
                return True
            async def close(self):
                pass
        async def scenario():
            with patch("app.t100_live_ops_notifier.T100LiveOpsNotifier", FakeNotifier):
                await run_sentinel(environ=self.env, cycles=1)
                await run_sentinel(environ=self.env, cycles=1)
        asyncio.run(scenario())
        self.assertEqual(events, ["WATCH_HEARTBEAT_JOURNAL_UNAVAILABLE"])


if __name__ == "__main__":
    unittest.main()
