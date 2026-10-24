"""Offline tests for bounded read-only soak evidence and kill behavior."""
import unittest
from decimal import Decimal as D

from app.t100_live_soak import (
    DiscoverySoakConfig,
    OperationalSoakController,
    RawDiscoverySoakRunner,
    SoakHalted,
)
from app.t100_live_shadow_session import ShadowObservation
from app.t100_live_supervisor import ManagedShort


def managed():
    return ManagedShort(
        "SIREN_USDT", "10", D("20"), D("100"),
        "99", D("175"), D("0"), False,
    )


def observation(t=1000, now=None, **changes):
    payload = dict(
        positions=[{
            "symbol": "SIREN_USDT", "positionId": "10",
            "positionType": 2, "holdVol": "20", "state": 1,
        }],
        stops=[{
            "positionId": "10", "id": "99", "state": 1,
            "stopLossPrice": "175", "vol": "20",
        }],
        expected=[managed()],
        observed_at_ms=t,
        now_ms=t + 500 if now is None else now,
        positions_complete=True,
        stops_complete=True,
    )
    payload.update(changes)
    return ShadowObservation(**payload)


class FakeClock:
    def __init__(self, *, start_ns=1_000_000_000, step_ns=10_000_000):
        self.value = start_ns
        self.step = step_ns

    def __call__(self):
        current = self.value
        self.value += self.step
        return current


class FakeProbe:
    def __init__(self, position_reads, stop_reads, *, fail_at_position=None):
        self.position_reads = list(position_reads)
        self.stop_reads = list(stop_reads)
        self.position_calls = 0
        self.stop_calls = 0
        self.fail_at_position = fail_at_position

    async def raw_open_positions(self):
        self.position_calls += 1
        if self.fail_at_position == self.position_calls:
            raise TimeoutError("read failed")
        return self.position_reads.pop(0)

    async def raw_open_stops(self):
        self.stop_calls += 1
        return self.stop_reads.pop(0)


async def no_sleep(_seconds):
    return None


def pos(vol="20"):
    return [{
        "symbol": "SIREN_USDT", "positionId": "10",
        "positionType": 2, "holdVol": vol, "state": 1,
    }]


def stop():
    return [{
        "positionId": "10", "id": "99", "state": 1,
        "stopLossPrice": "175", "vol": "20",
    }]


class DiscoverySoakTests(unittest.IsolatedAsyncioTestCase):
    async def test_bounded_stable_raw_soak_completes_non_authorizing(self):
        probe = FakeProbe(
            [pos(), pos(), pos(), pos(), pos(), pos()],
            [stop(), stop(), stop(), stop(), stop(), stop()],
        )
        runner = RawDiscoverySoakRunner(
            probe,
            DiscoverySoakConfig(
                samples=3, interval_seconds=0, max_runtime_seconds=10),
        )
        report = await runner.run(sleep=no_sleep, monotonic_ns=FakeClock())
        self.assertEqual(report.completed_samples, 3)
        self.assertEqual(report.consistent_samples, 3)
        self.assertEqual(report.position_presence_samples, 3)
        self.assertEqual(report.current_tpsl_presence_samples, 3)
        self.assertEqual(report.position_without_current_tpsl_samples, 0)
        self.assertFalse(report.kill_tripped)
        self.assertEqual(report.status, "COMPLETED_READ_ONLY_DISCOVERY")
        self.assertFalse(report.raw_inventory_completeness_verified)
        self.assertFalse(report.exchange_atomicity_verified)
        self.assertFalse(report.entry_allowed)
        self.assertFalse(report.live_authorization)

    async def test_position_without_current_tpsl_is_counted_not_hidden(self):
        probe = FakeProbe(
            [pos(), pos(), pos(), pos()],
            [[], [], [], []],
        )
        runner = RawDiscoverySoakRunner(
            probe,
            DiscoverySoakConfig(
                samples=2, interval_seconds=0, max_runtime_seconds=10),
        )
        report = await runner.run(sleep=no_sleep, monotonic_ns=FakeClock())
        self.assertEqual(report.position_without_current_tpsl_samples, 2)
        self.assertEqual(report.current_tpsl_presence_samples, 0)
        self.assertFalse(report.kill_tripped)
        self.assertFalse(report.entry_allowed)

    async def test_cross_sample_count_transition_is_reported(self):
        probe = FakeProbe(
            [pos(), pos(), pos(), pos()],
            [[], [], stop(), stop()],
        )
        runner = RawDiscoverySoakRunner(
            probe,
            DiscoverySoakConfig(
                samples=2, interval_seconds=0, max_runtime_seconds=10),
        )
        report = await runner.run(sleep=no_sleep, monotonic_ns=FakeClock())
        self.assertEqual(report.cross_sample_inventory_count_changes, 1)
        self.assertEqual(report.position_without_current_tpsl_samples, 1)
        self.assertEqual(report.current_tpsl_presence_samples, 1)

    async def test_volatile_raw_field_drift_is_reported_not_killed(self):
        p1 = pos()
        p1[0]["unRealizedPnl"] = "1.0"
        p2 = pos()
        p2[0]["unRealizedPnl"] = "1.5"
        p3 = pos()
        p3[0]["unRealizedPnl"] = "2.0"
        p4 = pos()
        p4[0]["unRealizedPnl"] = "2.5"
        probe = FakeProbe(
            [p1, p2, p3, p4],
            [stop(), stop(), stop(), stop()],
        )
        runner = RawDiscoverySoakRunner(
            probe,
            DiscoverySoakConfig(
                samples=2, interval_seconds=0, max_runtime_seconds=10),
        )
        report = await runner.run(sleep=no_sleep, monotonic_ns=FakeClock())
        self.assertFalse(report.kill_tripped)
        self.assertEqual(report.full_raw_drift_samples, 2)
        self.assertIn(
            "unRealizedPnl", report.changed_position_field_names)
        self.assertEqual(report.status, "COMPLETED_READ_ONLY_DISCOVERY")

    async def test_within_pair_inventory_drift_trips_kill(self):
        probe = FakeProbe(
            [pos("20"), pos("19")],
            [stop(), stop()],
        )
        runner = RawDiscoverySoakRunner(
            probe,
            DiscoverySoakConfig(
                samples=2, interval_seconds=0, max_runtime_seconds=10),
        )
        report = await runner.run(sleep=no_sleep, monotonic_ns=FakeClock())
        self.assertTrue(report.kill_tripped)
        self.assertEqual(
            report.kill_reason, "INVENTORY_CHANGED_BETWEEN_READS")
        self.assertEqual(report.completed_samples, 0)
        self.assertEqual(report.status, "HALTED_READ_ONLY_DISCOVERY")

    async def test_transport_failure_trips_read_failure(self):
        probe = FakeProbe(
            [pos(), pos(), pos(), pos()],
            [stop(), stop(), stop(), stop()],
            fail_at_position=2,
        )
        runner = RawDiscoverySoakRunner(
            probe,
            DiscoverySoakConfig(
                samples=2, interval_seconds=0, max_runtime_seconds=10),
        )
        report = await runner.run(sleep=no_sleep, monotonic_ns=FakeClock())
        self.assertTrue(report.kill_tripped)
        self.assertEqual(report.kill_reason, "READ_FAILURE")

    async def test_runtime_bound_trips_timeout(self):
        clock = FakeClock(step_ns=2_000_000_000)
        probe = FakeProbe(
            [pos(), pos(), pos(), pos()],
            [stop(), stop(), stop(), stop()],
        )
        runner = RawDiscoverySoakRunner(
            probe,
            DiscoverySoakConfig(
                samples=2, interval_seconds=0, max_runtime_seconds=1),
        )
        report = await runner.run(sleep=no_sleep, monotonic_ns=clock)
        self.assertTrue(report.kill_tripped)
        self.assertEqual(report.kill_reason, "READ_TIMEOUT")

    async def test_config_bounds_fail_closed(self):
        for samples in (0, 1, 241, True):
            with self.assertRaises(ValueError):
                DiscoverySoakConfig(samples=samples)
        for interval in (-1, 61, True):
            with self.assertRaises(ValueError):
                DiscoverySoakConfig(interval_seconds=interval)
        for runtime in (0, 3601, True):
            with self.assertRaises(ValueError):
                DiscoverySoakConfig(max_runtime_seconds=runtime)
        for sep in (0, 3001, True):
            with self.assertRaises(ValueError):
                DiscoverySoakConfig(max_pair_separation_ms=sep)


class OperationalSoakTests(unittest.TestCase):
    def test_threshold_accumulates_but_never_authorizes(self):
        soak = OperationalSoakController(
            minimum_observations=3, max_gap_ms=1000)
        r1 = soak.observe(observation(1000))
        r2 = soak.observe(observation(1500))
        r3 = soak.observe(observation(2000))
        self.assertFalse(r1.soak_threshold_met)
        self.assertFalse(r2.soak_threshold_met)
        self.assertTrue(r3.soak_threshold_met)
        self.assertEqual(r3.status, "SOAK_THRESHOLD_MET_NON_AUTHORIZING")
        self.assertFalse(r3.entry_allowed)
        self.assertFalse(r3.live_authorization)
        self.assertFalse(soak.kill.tripped)

    def test_incomplete_inventory_trips_permanent_kill(self):
        soak = OperationalSoakController(minimum_observations=2)
        with self.assertRaisesRegex(
            SoakHalted, "INVENTORY_COMPLETENESS_UNVERIFIED"):
            soak.observe(observation(stops_complete=False))
        self.assertTrue(soak.kill.tripped)
        self.assertEqual(
            soak.kill.first_reason, "INVENTORY_COMPLETENESS_UNVERIFIED")
        with self.assertRaisesRegex(Exception, "KILL_LATCHED"):
            soak.observe(observation(2000))

    def test_stale_observation_trips_kill(self):
        soak = OperationalSoakController(minimum_observations=2)
        with self.assertRaisesRegex(SoakHalted, "STALE_OBSERVATION"):
            soak.observe(observation(1000, now=3000))
        self.assertEqual(soak.kill.first_reason, "STALE_OBSERVATION")

    def test_nonmonotonic_observation_trips_kill(self):
        soak = OperationalSoakController(minimum_observations=2)
        soak.observe(observation(1000))
        with self.assertRaisesRegex(
            SoakHalted, "NONMONOTONIC_OBSERVATION"):
            soak.observe(observation(1000))
        self.assertEqual(
            soak.kill.first_reason, "NONMONOTONIC_OBSERVATION")

    def test_observation_gap_trips_kill(self):
        soak = OperationalSoakController(
            minimum_observations=2, max_gap_ms=1000)
        soak.observe(observation(1000))
        with self.assertRaisesRegex(SoakHalted, "OBSERVATION_GAP"):
            soak.observe(observation(2001))
        self.assertEqual(soak.kill.first_reason, "OBSERVATION_GAP")

    def test_missing_stop_is_native_protection_lost(self):
        soak = OperationalSoakController(minimum_observations=2)
        with self.assertRaisesRegex(
            SoakHalted, "NATIVE_PROTECTION_LOST"):
            soak.observe(observation(stops=[]))
        self.assertEqual(
            soak.kill.first_reason, "NATIVE_PROTECTION_LOST")

    def test_extra_stop_is_native_protection_drift(self):
        soak = OperationalSoakController(minimum_observations=2)
        stops = list(observation().stops)
        stops.append({
            "positionId": "10", "id": "100", "state": 1,
            "stopLossPrice": "175", "vol": "20",
        })
        with self.assertRaisesRegex(
            SoakHalted, "NATIVE_PROTECTION_DRIFT"):
            soak.observe(observation(stops=stops))
        self.assertEqual(
            soak.kill.first_reason, "NATIVE_PROTECTION_DRIFT")

    def test_position_count_drift_trips_position_drift(self):
        soak = OperationalSoakController(minimum_observations=2)
        with self.assertRaisesRegex(SoakHalted, "POSITION_DRIFT"):
            soak.observe(observation(positions=[]))
        self.assertEqual(soak.kill.first_reason, "POSITION_DRIFT")

    def test_stop_price_drift_latches_native_protection(self):
        soak = OperationalSoakController(minimum_observations=2)
        bad = [{
            "positionId": "10", "id": "99", "state": 1,
            "stopLossPrice": "176", "vol": "20",
        }]
        with self.assertRaisesRegex(
            SoakHalted, "NATIVE_PROTECTION_DRIFT"):
            soak.observe(observation(stops=bad))
        self.assertEqual(
            soak.kill.first_reason, "NATIVE_PROTECTION_DRIFT")

    def test_position_volume_drift_latches_position(self):
        soak = OperationalSoakController(minimum_observations=2)
        bad = [{
            "symbol": "SIREN_USDT", "positionId": "10",
            "positionType": 2, "holdVol": "19", "state": 1,
        }]
        with self.assertRaisesRegex(SoakHalted, "POSITION_DRIFT"):
            soak.observe(observation(positions=bad))
        self.assertEqual(soak.kill.first_reason, "POSITION_DRIFT")

    def test_malformed_expected_state_trips_recovery_unverified(self):
        soak = OperationalSoakController(minimum_observations=2)
        bad = observation(expected=None)
        with self.assertRaisesRegex(
            SoakHalted, "RECOVERY_UNVERIFIED"):
            soak.observe(bad)
        self.assertEqual(
            soak.kill.first_reason, "RECOVERY_UNVERIFIED")

    def test_report_preserves_first_failure_and_non_authorizing(self):
        soak = OperationalSoakController(minimum_observations=2)
        with self.assertRaises(SoakHalted):
            soak.observe(observation(stops=[]))
        r = soak.report()
        self.assertTrue(r.kill_tripped)
        self.assertEqual(r.kill_reason, "NATIVE_PROTECTION_LOST")
        self.assertFalse(r.soak_threshold_met)
        self.assertFalse(r.entry_allowed)
        self.assertFalse(r.live_authorization)

    def test_invalid_operational_config_rejected(self):
        for minimum in (0, 1, 100001, True):
            with self.assertRaises(ValueError):
                OperationalSoakController(minimum_observations=minimum)
