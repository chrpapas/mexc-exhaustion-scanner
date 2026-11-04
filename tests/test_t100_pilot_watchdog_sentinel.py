"""Pure stdlib tests for independent sentinel durable liveness alarms."""
from __future__ import annotations

import asyncio
import sqlite3
import tempfile
import time
import unittest
from pathlib import Path

from app.t100_pilot_watchdog_heartbeat import WatchdogHeartbeat
from app.t100_pilot_watchdog_observer import PilotWatchVerdict
from app.t100_pilot_watchdog_runtime import PilotWatchdogRunner
from app.t100_pilot_watchdog_sentinel import (
    PilotLivenessSentinel, SentinelHalted,
)

SCOPE = "e" * 64


class SentinelTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.watched = Path(self.tmp.name) / "watched.sqlite"
        self.outbox = Path(self.tmp.name) / "sentinel.sqlite"
        self.sent = []
        self.can_send = True
        self.clock = None

    def tearDown(self):
        self.tmp.cleanup()

    async def notify(self, code):
        self.sent.append(code)
        return self.can_send

    def open(self, **kw):
        args = dict(
            watched_journal=self.watched, outbox_journal=self.outbox,
            expected_scope_sha256=SCOPE, notifier=self.notify,
            stale_after_ms=30000, notify_timeout_seconds=0.2,
        )
        args.update(kw)
        return PilotLivenessSentinel(**args)

    def tick(self, sentinel):
        return asyncio.run(sentinel.poll_once())

    def watched_instance(self, *, report_alarm=False):
        async def observer():
            if report_alarm:
                return PilotWatchVerdict(status="WATCH_UNPROTECTED_EXPOSURE_NOW")
            return PilotWatchVerdict(
                status="WATCH_FLAT_INVENTORY_OBSERVED_NONAUTHORIZING",
                alert_supervisor=False, account_flat_observed=True,
            )
        async def send(_code):
            return True
        return PilotWatchdogRunner(
            self.watched, scope_sha256=SCOPE,
            observer=observer, notifier=send,
        )

    def queue(self):
        with sqlite3.connect(self.outbox) as db:
            return db.execute(
                "SELECT code, attempts, delivered_ms FROM sentinel_outbox ORDER BY seq"
            ).fetchall()

    def test_dead_watchdog_missing_journal_creates_durable_alert(self):
        with self.open() as s:
            result = self.tick(s)
            self.assertEqual(result.status, "WATCH_HEARTBEAT_JOURNAL_UNAVAILABLE")
            self.assertTrue(result.alarm_latched)
            self.assertEqual(result.notifications_pending, 0)
            self.assertEqual(self.sent, ["WATCH_HEARTBEAT_JOURNAL_UNAVAILABLE"])
            self.assertFalse(self.watched.exists())
            self.assertFalse(result.mexc_orders_sent)
            self.assertFalse(result.emergency_close_executed)
            self.assertFalse(result.live_authorization)
        self.assertEqual(len(self.queue()), 1)

    def test_healthy_heartbeat_creates_no_alarm_and_never_modifies_watched_state(self):
        with self.watched_instance() as runner:
            asyncio.run(runner.poll_once())
            with sqlite3.connect(self.watched) as db:
                previous = db.execute("SELECT * FROM state").fetchone()
            with self.open() as s:
                v = self.tick(s)
                self.assertEqual(v.status, "WATCH_HEARTBEAT_RECENT_NONAUTHORIZING")
                self.assertFalse(v.alarm_latched)
                self.assertFalse(v.notifications_pending)
            with sqlite3.connect(self.watched) as db:
                self.assertEqual(db.execute("SELECT * FROM state").fetchone(), previous)
        self.assertEqual(self.sent, [])

    def test_stale_after_shutdown_alerts_and_deduplicates(self):
        with self.watched_instance() as runner:
            asyncio.run(runner.poll_once())
        future = int(time.time() * 1000) + 40000
        with self.open(clock_ms=lambda: future) as s:
            result = self.tick(s)
            self.assertEqual(result.status, "WATCH_HEARTBEAT_STALE")
            self.assertTrue(result.alarm_latched)
            self.tick(s)
        self.assertEqual(self.sent, ["WATCH_HEARTBEAT_STALE"])
        self.assertEqual(len(self.queue()), 1)

    def test_notification_failed_then_delivered_after_restart(self):
        self.can_send = False
        with self.open() as s:
            result = self.tick(s)
            self.assertEqual(result.notifications_pending, 1)
            self.assertEqual(self.queue()[0][1], 1)
        self.can_send = True
        with self.open() as s:
            result = self.tick(s)
            self.assertEqual(result.notifications_pending, 0)
            self.assertEqual(result.notifications_delivered_this_tick, 1)
            self.assertTrue(result.alarm_latched)
        self.assertEqual(self.sent, [
            "WATCH_HEARTBEAT_JOURNAL_UNAVAILABLE",
            "WATCH_HEARTBEAT_JOURNAL_UNAVAILABLE",
        ])
        self.assertEqual(len(self.queue()), 1)
        self.assertEqual(self.queue()[0][1], 2)

    def test_recovery_is_not_auto_ack_and_new_outage_produces_new_alarm(self):
        with self.open() as s:
            first = self.tick(s)
            self.assertTrue(first.alarm_latched)
            with self.watched_instance() as runner:
                asyncio.run(runner.poll_once())
            recovered = self.tick(s)
            self.assertEqual(recovered.status, "WATCH_HEARTBEAT_RECENT_NONAUTHORIZING")
            self.assertTrue(recovered.alarm_latched)
            with sqlite3.connect(self.watched) as db:
                db.execute("UPDATE state SET last_ms=last_ms-60000")
            again = self.tick(s)
            self.assertEqual(again.status, "WATCH_HEARTBEAT_STALE")
            self.assertTrue(again.alarm_latched)
        self.assertEqual(len(self.queue()), 2)

    def test_local_lock_prevents_second_sentinel_instance(self):
        with self.open():
            with self.assertRaisesRegex(SentinelHalted, "SENTINEL_JOURNAL_OR_LOCK_UNAVAILABLE"):
                self.open()

    def test_bound_scope_does_not_allow_rearm_on_credential_or_watch_path_change(self):
        with self.open() as s:
            self.tick(s)
        with self.assertRaisesRegex(SentinelHalted, "SENTINEL_SCOPE_MISMATCH"):
            self.open(expected_scope_sha256="d" * 64)
        other = Path(self.tmp.name) / "other-watched.sqlite"
        with self.assertRaisesRegex(SentinelHalted, "SENTINEL_SCOPE_MISMATCH"):
            self.open(watched_journal=other)
        with self.open() as s:
            self.assertTrue(self.tick(s).alarm_latched)

    def test_corrupt_existing_sentinel_without_scope_never_rearms(self):
        with sqlite3.connect(self.outbox) as db:
            db.execute("CREATE TABLE unexpected (x INTEGER)")
        with self.assertRaisesRegex(SentinelHalted, "SENTINEL_PRIOR_SCOPE_MISSING"):
            self.open()
        with sqlite3.connect(self.outbox) as db:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM sentinel_state").fetchone()[0], 0)

    def test_clock_regression_is_durable_and_returned(self):
        now = int(time.time() * 1000)
        ticks = iter([now, now - 1000, now])
        with self.watched_instance() as runner:
            asyncio.run(runner.poll_once())
            with self.open(clock_ms=lambda: next(ticks)) as s:
                self.assertFalse(self.tick(s).alarm_latched)
                result = self.tick(s)
                self.assertEqual(result.status, "WATCH_SENTINEL_CLOCK_REGRESSION")
                self.assertTrue(result.alarm_latched)
        self.assertEqual(self.sent, ["WATCH_SENTINEL_CLOCK_REGRESSION"])

    def test_invalid_or_forged_healthy_report_alerts(self):
        with self.open(
            inspector=lambda *_a, **_kw: WatchdogHeartbeat(
                status="WATCH_HEARTBEAT_RECENT_NONAUTHORIZING", alert=True
            )
        ) as s:
            result = self.tick(s)
            self.assertEqual(result.status, "WATCH_SENTINEL_HEARTBEAT_INVALID")
            self.assertTrue(result.alarm_latched)
        self.assertEqual(self.sent, ["WATCH_SENTINEL_HEARTBEAT_INVALID"])

    def test_bad_inspector_exception_is_redacted(self):
        def leak(*_a, **_kw):
            raise RuntimeError("PRIVATE_API_SECRET_IN_ERROR")
        with self.open(inspector=leak) as s:
            self.assertEqual(
                self.tick(s).status, "WATCH_SENTINEL_HEARTBEAT_INSPECTION_FAILED"
            )
        self.assertNotIn(b"PRIVATE_API_SECRET", self.outbox.read_bytes())
        self.assertNotIn("PRIVATE_API_SECRET", "".join(self.sent))

    def test_run_bounded_and_readonly(self):
        with self.open(poll_seconds=0.001) as s:
            result = asyncio.run(s.run(max_cycles=2))
        self.assertEqual(result.status, "WATCH_HEARTBEAT_JOURNAL_UNAVAILABLE")
        self.assertFalse(result.entry_allowed)
        self.assertEqual(len(self.sent), 1)

    def test_symlink_same_journal_and_relative_path_fail_closed(self):
        with self.assertRaisesRegex(SentinelHalted, "SENTINEL_JOURNAL_SCOPE_INVALID"):
            self.open(outbox_journal=self.watched)
        with self.assertRaisesRegex(SentinelHalted, "SENTINEL_JOURNAL_SCOPE_INVALID"):
            self.open(outbox_journal="relative.sqlite")
        link = Path(self.tmp.name) / "link.sqlite"
        link.symlink_to(self.outbox)
        with self.assertRaisesRegex(SentinelHalted, "SENTINEL_OUTBOX_JOURNAL_UNSAFE"):
            self.open(outbox_journal=link)


if __name__ == "__main__":
    unittest.main()
