"""End-to-end OFFLINE OS process death/restart drill. No MEXC/Discord/network.

Primary watcher and sentinel run in distinct real Python subprocesses.
The watcher has an injected simulated FLAT verdict; the sentinel delivers
redacted alarms to a local mock receiver file. Do not use this as proof of
live MEXC native-stop protection, actual Discord delivery or deployment.
"""
from __future__ import annotations

import json
import os
import sqlite3
import subprocess
import sys
import tempfile
import textwrap
import time
import unittest
from pathlib import Path

SCOPE = "b" * 64

WATCH_PROCESS = r"""
import asyncio
import sys

from app.t100_pilot_watchdog_observer import PilotWatchVerdict
from app.t100_pilot_watchdog_runtime import PilotWatchdogRunner

async def read_simulated_flat():
    return PilotWatchVerdict(
        status="WATCH_FLAT_INVENTORY_OBSERVED_NONAUTHORIZING",
        alert_supervisor=False, account_flat_observed=True,
    )

async def local_mock_notifier(_code):
    return True

with PilotWatchdogRunner(
    sys.argv[1], scope_sha256=sys.argv[2],
    observer=read_simulated_flat, notifier=local_mock_notifier,
    poll_seconds=0.05,
) as runner:
    asyncio.run(runner.run(max_cycles=None))
"""

SENTINEL_PROCESS = r"""
import asyncio
import json
import sys

from app.t100_pilot_watchdog_sentinel import PilotLivenessSentinel

async def local_mock_notifier(code):
    if sys.argv[4] == "fail":
        return False
    # Record only a redacted WATCH_* event in a local mock receiver.
    # This MUST NOT be confused with a real Discord webhook delivery.
    with open(sys.argv[5], "a", encoding="utf-8") as stream:
        stream.write(code + "\n")
        stream.flush()
    return True

with PilotLivenessSentinel(
    watched_journal=sys.argv[1],
    outbox_journal=sys.argv[2],
    expected_scope_sha256=sys.argv[3],
    stale_after_ms=1000,
    poll_seconds=0.05,
    notify_timeout_seconds=1,
    notifier=local_mock_notifier,
) as sentinel:
    report = asyncio.run(sentinel.run(max_cycles=1))
print(json.dumps({
    "status": report.status,
    "alarm_latched": report.alarm_latched,
    "pending": report.notifications_pending,
    "delivered": report.notifications_delivered_this_tick,
    "mexc_orders_sent": report.mexc_orders_sent,
    "live_authorization": report.live_authorization,
}, sort_keys=True))
sys.exit(2 if report.alarm_latched or report.notifications_pending else 0)
"""


class IndependentProcessFailureDrill(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        base = Path(self.tmp.name)
        self.watched = base / "simulated-primary.sqlite"
        self.outbox = base / "simulated-sentinel.sqlite"
        self.receiver = base / "mock-receiver.log"
        self.children = []

    def tearDown(self):
        for child in self.children:
            if child.poll() is None:
                child.kill()
            try:
                child.communicate(timeout=5)
            except subprocess.TimeoutExpired:
                child.kill()
                child.communicate(timeout=5)
        self.tmp.cleanup()

    @staticmethod
    def command(source, *args):
        return [sys.executable, "-c", textwrap.dedent(source), *map(str, args)]

    def start_primary(self):
        child = subprocess.Popen(
            self.command(WATCH_PROCESS, self.watched, SCOPE),
            stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        )
        self.children.append(child)
        deadline = time.monotonic() + 8.0
        while time.monotonic() < deadline:
            if child.poll() is not None:
                out, err = child.communicate(timeout=2)
                self.fail("simulated primary exited early: " + err.decode()[-1000:])
            if self.watched.is_file():
                try:
                    with sqlite3.connect(self.watched, timeout=0.5) as db:
                        rev = db.execute(
                            "SELECT revision FROM state WHERE id=1"
                        ).fetchone()
                    if rev and rev[0] >= 2:
                        return child
                except sqlite3.Error:
                    pass
            time.sleep(0.04)
        self.fail("simulated primary did not create two healthy journal ticks")

    def one_sentinel_process(self, mode):
        run = subprocess.run(
            self.command(
                SENTINEL_PROCESS, self.watched, self.outbox, SCOPE,
                mode, self.receiver,
            ),
            capture_output=True, text=True, timeout=10,
        )
        self.assertIn(run.returncode, (0, 2), run.stderr[-1200:])
        try:
            report = json.loads(run.stdout)
        except ValueError:
            self.fail("sentinel subprocess had invalid report: " + run.stdout[-700:])
        self.assertFalse(report["mexc_orders_sent"])
        self.assertFalse(report["live_authorization"])
        return run.returncode, report

    def delivered_events(self):
        if not self.receiver.exists():
            return []
        return self.receiver.read_text().splitlines()

    def test_real_process_death_durable_retry_restart_and_no_duplicate_alarm(self):
        primary = self.start_primary()
        rc, healthy = self.one_sentinel_process("deliver")
        self.assertEqual(rc, 0)
        self.assertEqual(healthy["status"], "WATCH_HEARTBEAT_RECENT_NONAUTHORIZING")
        self.assertFalse(healthy["alarm_latched"])
        self.assertEqual(self.delivered_events(), [])

        # Actual OS SIGKILL: the primary cannot execute cleanup/notifications.
        primary.kill()
        primary.communicate(timeout=5)
        # The observer's last retained tick becomes stale on real wall time.
        time.sleep(1.3)

        rc, failed = self.one_sentinel_process("fail")
        self.assertEqual(rc, 2)
        self.assertEqual(failed["status"], "WATCH_HEARTBEAT_STALE")
        self.assertTrue(failed["alarm_latched"])
        self.assertEqual(failed["pending"], 1)
        self.assertEqual(self.delivered_events(), [])

        # An ENTIRELY NEW sentinel process delivers the previously committed
        # alarm. It may deliver at least once; no acknowledgment exists until
        # the receiver returns true and the SQLite row is committed.
        rc, recovered = self.one_sentinel_process("deliver")
        self.assertEqual(rc, 2)
        self.assertEqual(recovered["status"], "WATCH_HEARTBEAT_STALE")
        self.assertEqual(recovered["pending"], 0)
        self.assertEqual(recovered["delivered"], 1)
        self.assertEqual(self.delivered_events(), ["WATCH_HEARTBEAT_STALE"])

        rc, repeated = self.one_sentinel_process("deliver")
        self.assertEqual(rc, 2)  # permanent latch: no silent re-arm
        self.assertEqual(repeated["pending"], 0)
        self.assertEqual(repeated["delivered"], 0)
        self.assertEqual(self.delivered_events(), ["WATCH_HEARTBEAT_STALE"])

        # Restart primary from persisted primary journal. This does not
        # resolve the sentinel's historical alarm or send duplicate messages.
        restarted_primary = self.start_primary()
        rc, resumed = self.one_sentinel_process("deliver")
        self.assertEqual(rc, 2)
        self.assertEqual(resumed["status"], "WATCH_HEARTBEAT_RECENT_NONAUTHORIZING")
        self.assertTrue(resumed["alarm_latched"])
        self.assertEqual(self.delivered_events(), ["WATCH_HEARTBEAT_STALE"])
        restarted_primary.kill()
        restarted_primary.communicate(timeout=5)

        with sqlite3.connect(self.outbox) as db:
            rows = db.execute(
                "SELECT code,attempts,delivered_ms FROM sentinel_outbox"
            ).fetchall()
            self.assertEqual(len(rows), 1)
            self.assertEqual(rows[0][0], "WATCH_HEARTBEAT_STALE")
            self.assertEqual(rows[0][1], 2)
            self.assertIsNotNone(rows[0][2])


if __name__ == "__main__":
    unittest.main()
