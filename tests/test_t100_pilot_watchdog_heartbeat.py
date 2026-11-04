"""Liveness sentinel tests: read-only journal inspection, no MEXC calls."""
from __future__ import annotations

import asyncio
import tempfile
import time
import unittest
from pathlib import Path

from app.t100_pilot_watchdog_heartbeat import inspect_watchdog_heartbeat
from app.t100_pilot_watchdog_observer import PilotWatchVerdict
from app.t100_pilot_watchdog_runtime import PilotWatchdogRunner

SCOPE = "f" * 64


class ExternalHeartbeatTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name) / "separate-watchdog.sqlite"

    def tearDown(self):
        self.temp.cleanup()

    def check(self, now_ms=None, **kw):
        return inspect_watchdog_heartbeat(
            self.path, expected_scope_sha256=SCOPE,
            now_ms=now_ms if now_ms is not None else int(time.time()*1000),
            **kw,
        )

    def create(self, *, alert=False):
        async def observer():
            if alert:
                return PilotWatchVerdict(status="WATCH_UNPROTECTED_EXPOSURE_NOW")
            return PilotWatchVerdict(
                status="WATCH_FLAT_INVENTORY_OBSERVED_NONAUTHORIZING",
                alert_supervisor=False, account_flat_observed=True,
            )
        async def notifier(_):
            return not alert
        runner = PilotWatchdogRunner(
            self.path, scope_sha256=SCOPE,
            observer=observer, notifier=notifier,
        )
        return runner

    def test_missing_journal_is_an_alarm_without_file_creation(self):
        result = self.check()
        self.assertEqual(result.status, "WATCH_HEARTBEAT_JOURNAL_UNAVAILABLE")
        self.assertTrue(result.alert)
        self.assertFalse(self.path.exists())

    def test_created_but_not_started_is_not_healthy(self):
        with self.create():
            result = self.check()
            self.assertEqual(result.status, "WATCH_HEARTBEAT_NEVER_STARTED")
            self.assertTrue(result.alert)

    def test_recent_started_observer_is_non_authorizing(self):
        with self.create() as runner:
            asyncio.run(runner.poll_once())
            result = self.check()
            self.assertEqual(result.status, "WATCH_HEARTBEAT_RECENT_NONAUTHORIZING")
            self.assertFalse(result.alert)
            self.assertEqual(result.revision, 1)
            self.assertEqual(result.unacknowledged_alarms, 0)
            self.assertFalse(result.live_authorization)
            self.assertFalse(result.mexc_orders_sent)

    def test_stale_heartbeat_alerts_when_watchdog_stops(self):
        with self.create() as runner:
            asyncio.run(runner.poll_once())
            result = self.check(now_ms=int(time.time()*1000)+45000)
            self.assertEqual(result.status, "WATCH_HEARTBEAT_STALE")
            self.assertTrue(result.alert)

    def test_latched_alarm_remains_unhealthy_even_after_flat(self):
        with self.create(alert=True) as runner:
            asyncio.run(runner.poll_once())
        # A later flat observation does not auto-clear the alarm.
        with self.create() as runner:
            asyncio.run(runner.poll_once())
            result = self.check()
            self.assertEqual(result.status, "WATCH_HEARTBEAT_PREVIOUS_ALARM_LATCHED")
            self.assertTrue(result.alert)

    def test_scope_mismatch_and_future_clock_are_alarms(self):
        with self.create() as runner:
            asyncio.run(runner.poll_once())
            result = inspect_watchdog_heartbeat(
                self.path, expected_scope_sha256="b"*64,
                now_ms=int(time.time()*1000),
            )
            self.assertEqual(result.status, "WATCH_HEARTBEAT_SCOPE_MISMATCH")
            result = self.check(now_ms=int(time.time()*1000)-15000)
            self.assertEqual(result.status, "WATCH_HEARTBEAT_CLOCK_SKEW")

    def test_symlink_and_invalid_scope_fail_closed(self):
        with self.create():
            link = Path(self.temp.name) / "alias.sqlite"
            link.symlink_to(self.path)
            result = inspect_watchdog_heartbeat(
                link, expected_scope_sha256=SCOPE, now_ms=int(time.time()*1000),
            )
            self.assertEqual(result.status, "WATCH_HEARTBEAT_JOURNAL_UNSAFE")
            result = self.check(stale_after_ms=0)
            self.assertEqual(result.status, "WATCH_HEARTBEAT_CHECK_CONFIG_INVALID")
            result = inspect_watchdog_heartbeat(
                self.path, expected_scope_sha256="bad",
                now_ms=int(time.time()*1000),
            )
            self.assertEqual(result.status, "WATCH_HEARTBEAT_CHECK_CONFIG_INVALID")


if __name__ == "__main__":
    unittest.main()
