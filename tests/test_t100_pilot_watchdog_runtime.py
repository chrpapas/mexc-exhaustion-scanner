"""Mocked GET-only watchdog runtime tests; no credentials or exchange POST."""
from __future__ import annotations

import asyncio
import sqlite3
import tempfile
import time
import unittest
from pathlib import Path

from app.t100_pilot_watchdog_observer import PilotWatchVerdict
from app.t100_pilot_watchdog_runtime import (
    PilotWatchdogRunner, WatchdogHalted,
)

SCOPE = "a" * 64
SAFE = "WATCH_NATIVE_STOP_CURRENTLY_OBSERVED_NONAUTHORIZING"
FLAT = "WATCH_FLAT_INVENTORY_OBSERVED_NONAUTHORIZING"
UNPROTECTED = "WATCH_UNPROTECTED_EXPOSURE_NOW"


def safe():
    return PilotWatchVerdict(
        status=SAFE, alert_supervisor=False,
        currently_active_stop_observed=True,
    )


def flat():
    return PilotWatchVerdict(
        status=FLAT, alert_supervisor=False,
        account_flat_observed=True,
    )


def alarm():
    return PilotWatchVerdict(status=UNPROTECTED)


class WatchdogRuntimeTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.path = Path(self.directory.name) / "pilot-watchdog.sqlite"
        self.delivered = []
        self.allow_delivery = True
        self.next_verdict = safe()

    def tearDown(self):
        self.directory.cleanup()

    async def observe(self):
        return self.next_verdict

    async def notify(self, code):
        self.delivered.append(code)
        return self.allow_delivery

    def open(self, **overrides):
        args = dict(
            scope_sha256=SCOPE,
            observer=self.observe,
            notifier=self.notify,
            observation_timeout_seconds=0.5,
            notification_timeout_seconds=0.5,
        )
        args.update(overrides)
        return PilotWatchdogRunner(self.path, **args)

    def tick(self, runner):
        return asyncio.run(runner.poll_once())

    def counts(self):
        with sqlite3.connect(self.path) as db:
            return db.execute(
                "SELECT COUNT(*), SUM(CASE WHEN delivered_ms IS NULL THEN 1 ELSE 0 END) "
                "FROM outbox"
            ).fetchone()

    def test_current_native_stop_does_not_latch_alarm_or_send_notice(self):
        with self.open() as runner:
            result = self.tick(runner)
            self.assertEqual(result.status, SAFE)
            self.assertFalse(result.alarm_latched)
            self.assertEqual(result.notifications_pending, 0)
            self.assertFalse(result.entry_allowed)
            self.assertFalse(result.live_authorization)
            self.assertFalse(result.emergency_close_executed)
            self.assertFalse(result.mexc_orders_sent)
            self.assertTrue(result.read_only)
            self.assertEqual(self.delivered, [])

    def test_notification_failure_persists_and_retries_after_restart(self):
        self.next_verdict = alarm()
        self.allow_delivery = False
        with self.open() as runner:
            first = self.tick(runner)
            self.assertEqual(first.status, UNPROTECTED)
            self.assertTrue(first.alarm_latched)
            self.assertEqual(first.notifications_pending, 1)
            self.assertEqual(self.counts(), (1, 1))
        self.next_verdict = flat()
        self.allow_delivery = True
        with self.open() as runner:
            second = self.tick(runner)
            self.assertEqual(second.status, FLAT)
            self.assertTrue(second.alarm_latched)  # no automatic clearance
            self.assertEqual(second.notifications_pending, 0)
            self.assertEqual(second.notifications_delivered_this_tick, 1)
        self.assertEqual(self.delivered, [UNPROTECTED, UNPROTECTED])
        self.assertEqual(self.counts(), (1, 0))

    def test_repeated_alarm_is_deduplicated_until_state_transition(self):
        self.next_verdict = alarm()
        with self.open() as runner:
            self.tick(runner)
            self.tick(runner)
            self.assertEqual(self.counts(), (1, 0))
            self.next_verdict = flat()
            self.tick(runner)
            self.next_verdict = alarm()
            self.tick(runner)
            self.assertEqual(self.counts(), (2, 0))
        self.assertEqual(self.delivered, [UNPROTECTED, UNPROTECTED])

    def test_invalid_verdict_and_sensitive_exception_are_redacted(self):
        async def dangerous():
            raise RuntimeError("PRIVATE_RESPONSE_WITH_API_KEY")
        with self.open(observer=dangerous) as runner:
            result = self.tick(runner)
            self.assertEqual(result.status, "WATCH_RUNTIME_GET_TIMEOUT_OR_EXCEPTION")
            self.assertTrue(result.alarm_latched)
        content = self.path.read_bytes()
        self.assertNotIn(b"PRIVATE_RESPONSE_WITH_API_KEY", content)
        self.assertTrue(all("PRIVATE_" not in x for x in self.delivered))

    def test_invalid_success_claim_becomes_alarm(self):
        self.next_verdict = PilotWatchVerdict(status=SAFE, alert_supervisor=False)
        with self.open() as runner:
            result = self.tick(runner)
            self.assertEqual(result.status, "WATCH_RUNTIME_VERDICT_INVALID")
            self.assertTrue(result.alarm_latched)
        self.assertEqual(self.delivered, ["WATCH_RUNTIME_VERDICT_INVALID"])

    def test_timeout_is_a_durable_unknown_not_flatness(self):
        async def delayed():
            await asyncio.sleep(0.05)
            return flat()
        with self.open(
            observer=delayed, observation_timeout_seconds=0.001,
        ) as runner:
            result = self.tick(runner)
            self.assertEqual(result.status, "WATCH_RUNTIME_GET_TIMEOUT_OR_EXCEPTION")
            self.assertTrue(result.alarm_latched)
            self.assertFalse(result.live_authorization)

    def test_process_lock_and_scope_enforced_after_restart(self):
        with self.open() as runner:
            with self.assertRaisesRegex(WatchdogHalted, "WATCHDOG_JOURNAL_OR_LOCK_UNAVAILABLE"):
                self.open()
            self.tick(runner)
        with self.assertRaisesRegex(WatchdogHalted, "WATCHDOG_SCOPE_MISMATCH"):
            self.open(scope_sha256="b" * 64)
        with self.open() as runner:
            self.assertEqual(self.tick(runner).status, SAFE)

    def test_backward_clock_generates_alarm_and_does_not_reset_history(self):
        now = int(time.time() * 1000)
        moments = iter([now, now - 1000, now])
        with self.open(clock_ms=lambda: next(moments)) as runner:
            self.assertFalse(self.tick(runner).alarm_latched)
            result = self.tick(runner)
            self.assertEqual(result.status, "WATCH_RUNTIME_CLOCK_REGRESSION")
            self.assertTrue(result.alarm_latched)
        self.assertEqual(self.delivered, ["WATCH_RUNTIME_CLOCK_REGRESSION"])

    def test_notifier_exception_leaves_event_pending_and_is_retried(self):
        async def failing_notifier(code):
            raise RuntimeError("PRIVATE_WEBHOOK_SECRET")
        self.next_verdict = alarm()
        with self.open(notifier=failing_notifier) as runner:
            result = self.tick(runner)
            self.assertEqual(result.notifications_pending, 1)
        with self.open() as runner:
            result = self.tick(runner)
            self.assertEqual(result.notifications_pending, 0)
            self.assertTrue(result.alarm_latched)
        self.assertEqual(self.delivered, [UNPROTECTED])

    def test_symlink_and_nondurable_paths_fail_closed(self):
        linked = Path(self.directory.name) / "bad.sqlite"
        linked.symlink_to(self.path)
        with self.assertRaisesRegex(WatchdogHalted, "WATCHDOG_JOURNAL_PATH_UNSAFE"):
            PilotWatchdogRunner(
                linked, scope_sha256=SCOPE,
                observer=self.observe, notifier=self.notify,
            )
        with self.assertRaisesRegex(WatchdogHalted, "WATCHDOG_DURABLE_PATH_REQUIRED"):
            PilotWatchdogRunner(
                ":memory:", scope_sha256=SCOPE,
                observer=self.observe, notifier=self.notify,
            )

    def test_bounded_polling_loop_runs_without_order_transport(self):
        called = 0
        async def observe():
            nonlocal called
            called += 1
            return flat()
        with self.open(observer=observe, poll_seconds=0.001) as runner:
            asyncio.run(runner.run(max_cycles=3))
        self.assertEqual(called, 3)
        self.assertEqual(self.counts(), (0, None))

    def test_closing_runner_prevents_restart_without_reopening_journal(self):
        runner = self.open()
        runner.close()
        with self.assertRaisesRegex(WatchdogHalted, "WATCHDOG_CLOSED"):
            self.tick(runner)


if __name__ == "__main__":
    unittest.main()
