"""No-Mac, no-exchange offline tests for the real launchd sentinel restart drill."""
from __future__ import annotations

import importlib.util
import pathlib
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

SOURCE = pathlib.Path(__file__).resolve().parents[1] / "scripts" / "t100_pilot_mac_sentinel_restart_drill.py"
spec = importlib.util.spec_from_file_location("t100_restart_drill", SOURCE)
module = importlib.util.module_from_spec(spec)
import sys
sys.modules[spec.name] = module
spec.loader.exec_module(module)


class RestartDrillTests(unittest.TestCase):
    def setUp(self):
        self.output = []
        self.kill_calls = 0
        self.kick_calls = 0
        self.t = 0.0
        self.after_kill = False

    def tick(self, seconds):
        self.t += seconds

    def kill(self):
        self.kill_calls += 1
        self.after_kill = True
        return True

    def kick(self):
        self.kick_calls += 1
        return True

    def pid(self, label):
        self.assertEqual(label, module.SENTINEL_LABEL)
        return 1234 if not self.after_kill or self.t < 2 else 4567

    def sample(self):
        return (module.Sample(100, 600) if not self.after_kill or self.t < 2
                else module.Sample(100, 601))

    def exercise(self, **kw):
        args = dict(sample=self.sample, pid=self.pid, send_kill=self.kill,
                    kickstart=self.kick, now=lambda: self.t,
                    sleep=self.tick, emit=self.output.append,
                    max_wait=8, step=1)
        args.update(kw)
        return module.run_drill(**args)

    def test_verified_new_process_pid_and_journal_no_mexc_action(self):
        self.assertTrue(self.exercise())
        self.assertEqual(self.kill_calls, 1)
        self.assertEqual(self.kick_calls, 0)
        self.assertIn("SENTINEL_AUTO_RESTART=PASS_NEW_PROCESS_AND_FRESH_JOURNAL", self.output)
        self.assertIn("MEXC_REQUESTS_BY_DRILL=ZERO MEXC_MUTATIONS=ZERO LIVE_AUTHORIZATION=FALSE", self.output)

    def test_no_signal_on_unhealthy_precheck(self):
        with self.assertRaisesRegex(module.RestartDrillHalted, "MONITOR_UNHEALTHY"):
            self.exercise(sample=lambda: module.halt("MONITOR_UNHEALTHY"))
        self.assertEqual(self.kill_calls, 0)
        self.assertEqual(self.kick_calls, 0)

    def test_no_signal_when_old_pid_missing(self):
        with self.assertRaisesRegex(module.RestartDrillHalted, "SENTINEL_PID_NOT_VERIFIED"):
            self.exercise(pid=lambda _: None)
        self.assertEqual(self.kill_calls, 0)
        self.assertEqual(self.kick_calls, 0)

    def test_rejected_launchctl_kill_does_not_claim_success(self):
        with self.assertRaisesRegex(module.RestartDrillHalted, "SENTINEL_KILL_NOT_ACCEPTED"):
            self.exercise(send_kill=lambda: False)
        self.assertEqual(self.kick_calls, 0)

    def test_no_new_process_timeout_requests_scoped_recovery(self):
        with self.assertRaisesRegex(module.RestartDrillHalted, "AUTO_RESTART_NOT_VERIFIED"):
            self.exercise(pid=lambda _: 1234)
        self.assertEqual(self.kick_calls, 1)
        self.assertIn("SENTINEL_RECOVERY_KICKSTART=ACCEPTED", self.output)

    def test_new_pid_but_stale_journal_is_not_success(self):
        with self.assertRaisesRegex(module.RestartDrillHalted, "AUTO_RESTART_NOT_VERIFIED"):
            self.exercise(sample=lambda: module.Sample(100, 600))
        self.assertEqual(self.kick_calls, 1)

    def test_new_pid_but_unchanged_watcher_revision_is_acceptable(self):
        self.assertTrue(self.exercise())
        self.assertIn("AFTER_REVISIONS=WATCH_100_SENTINEL_601", self.output)

    def test_exception_during_postkill_sampling_attempts_recovery(self):
        calls = [0]
        def conditional():
            calls[0] += 1
            if calls[0] == 1:
                return module.Sample(100, 600)
            raise module.RestartDrillHalted("MONITOR_LATCHED")
        with self.assertRaisesRegex(module.RestartDrillHalted, "AUTO_RESTART_NOT_VERIFIED"):
            self.exercise(sample=conditional)
        self.assertGreaterEqual(calls[0], 2)
        self.assertEqual(self.kick_calls, 1)

    def test_invalid_timeout_prevents_signal(self):
        with self.assertRaisesRegex(module.RestartDrillHalted, "DRILL_TIMEOUT_INVALID"):
            self.exercise(max_wait=3600)
        self.assertEqual(self.kill_calls, 0)

    def test_discarding_postkill_journal_as_bad_never_claims_success(self):
        calls = [0]
        def broken():
            calls[0] += 1
            if calls[0] == 1:
                return module.Sample(100, 600)
            raise module.RestartDrillHalted("MONITOR_NOT_FLAT_HEALTHY_OR_LATCHED")
        with self.assertRaisesRegex(module.RestartDrillHalted, "AUTO_RESTART_NOT_VERIFIED"):
            self.exercise(sample=broken)
        self.assertEqual(self.kick_calls, 1)


class JournalReadOnlyTests(unittest.TestCase):
    def test_reads_old_journal_without_mutation_and_rejects_symlink(self):
        with tempfile.TemporaryDirectory() as td:
            p = pathlib.Path(td) / "sentinel.sqlite"
            with sqlite3.connect(p) as db:
                db.execute("CREATE TABLE state(id INT,binding TEXT,last_code TEXT,last_ms INT,revision INT,latched INT)")
                db.execute("CREATE TABLE outbox(seq INT,delivered_ms INT)")
                db.execute("INSERT INTO state VALUES (1, 'scope', 'WATCHER_RECENT_FLAT_NONAUTHORIZING', 10, 20, 0)")
            p.chmod(0o600)
            before = p.read_bytes()
            with patch.object(module.os, "getuid", return_value=p.stat().st_uid):
                row = module._journal(p)
                self.assertEqual(row, ('scope', 'WATCHER_RECENT_FLAT_NONAUTHORIZING', 10, 20, 0, 0))
                self.assertEqual(before, p.read_bytes())
                link = pathlib.Path(td) / "alias.sqlite"
                link.symlink_to(p)
                self.assertIsNone(module._journal(link))


if __name__ == "__main__":
    unittest.main()
