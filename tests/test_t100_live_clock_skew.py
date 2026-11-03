"""Offline public clock-skew policy tests."""
import unittest

from app.t100_live_clock_skew import (
    ClockSkewHalted,
    assess_clock_skew,
)


class ClockSkewTests(unittest.TestCase):
    def test_server_time_inside_local_request_interval_passes(self):
        r=assess_clock_skew(
            server_time_ms=1500,
            local_before_ms=1000,
            local_after_ms=2000,
        )
        self.assertTrue(r.within_limit)
        self.assertEqual(r.outside_local_interval_ms,0)
        self.assertFalse(r.live_authorization)

    def test_two_second_outside_boundary_passes(self):
        r=assess_clock_skew(
            server_time_ms=3000,
            local_before_ms=1000,
            local_after_ms=1000,
            max_outside_interval_ms=2000,
        )
        self.assertTrue(r.within_limit)

    def test_excess_clock_skew_blocks(self):
        r=assess_clock_skew(
            server_time_ms=3001,
            local_before_ms=1000,
            local_after_ms=1000,
            max_outside_interval_ms=2000,
        )
        self.assertFalse(r.within_limit)

    def test_slow_public_round_trip_blocks(self):
        r=assess_clock_skew(
            server_time_ms=2500,
            local_before_ms=1000,
            local_after_ms=4001,
            max_round_trip_ms=3000,
        )
        self.assertFalse(r.within_limit)

    def test_backward_local_clock_halts(self):
        with self.assertRaisesRegex(
            ClockSkewHalted,"MOVED_BACKWARD"
        ):
            assess_clock_skew(
                server_time_ms=1000,
                local_before_ms=2000,
                local_after_ms=1999,
            )


if __name__=="__main__":
    unittest.main()
