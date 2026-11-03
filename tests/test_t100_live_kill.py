"""Pure tests for irreversible operational kill state."""
import unittest

from app.t100_live_kill import (
    ALLOWED_REASONS,
    KillSwitchTripped,
    OperationalKillLatch,
)


class KillLatchTests(unittest.TestCase):
    def test_new_latch_is_non_authorizing(self):
        k = OperationalKillLatch()
        s = k.snapshot()
        self.assertFalse(s.tripped)
        self.assertIsNone(s.first_reason)
        self.assertEqual(s.trip_count, 0)
        self.assertFalse(s.entry_allowed)
        self.assertFalse(s.live_authorization)

    def test_first_trip_latches_reason(self):
        k = OperationalKillLatch()
        s = k.trip("READ_FAILURE")
        self.assertTrue(s.tripped)
        self.assertEqual(s.first_reason, "READ_FAILURE")
        self.assertEqual(s.trip_count, 1)

    def test_later_trip_never_replaces_first_reason(self):
        k = OperationalKillLatch()
        k.trip("READ_FAILURE")
        s = k.trip("NATIVE_PROTECTION_LOST")
        self.assertEqual(s.first_reason, "READ_FAILURE")
        self.assertEqual(s.trip_count, 2)

    def test_require_untripped_fails_after_trip(self):
        k = OperationalKillLatch()
        k.require_untripped()
        k.trip("STALE_OBSERVATION")
        with self.assertRaisesRegex(
            KillSwitchTripped, "STALE_OBSERVATION"):
            k.require_untripped()

    def test_unknown_reason_rejected_without_state_change(self):
        k = OperationalKillLatch()
        with self.assertRaisesRegex(ValueError, "UNKNOWN_KILL_REASON"):
            k.trip("IGNORE_ALL_RISK")
        self.assertFalse(k.tripped)
        self.assertEqual(k.snapshot().trip_count, 0)

    def test_all_declared_reasons_are_accepted(self):
        for reason in ALLOWED_REASONS:
            k = OperationalKillLatch()
            self.assertEqual(k.trip(reason).first_reason, reason)

    def test_no_reset_method_exists(self):
        k = OperationalKillLatch()
        self.assertFalse(hasattr(k, "reset"))
        self.assertFalse(hasattr(k, "clear"))
        self.assertFalse(hasattr(k, "enable"))
