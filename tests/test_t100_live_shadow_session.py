"""Offline sequential shadow tests; no live account or exchange access."""
import unittest
from decimal import Decimal as D
from app.t100_live_shadow_session import (
    ShadowObservation, ShadowSession, ShadowSessionHalted,
)
from app.t100_live_supervisor import ManagedShort


def managed():
    return ManagedShort("SIREN_USDT", "10", D("20"), D("100"), "99", D("175"), D("0"), False)


def observation(t=1000, now=None, **changes):
    payload = dict(
        positions=[dict(symbol="SIREN_USDT", positionId="10", positionType=2, holdVol="20", state=1)],
        stops=[dict(positionId="10", id="99", state=1, stopLossPrice="175", vol="20")],
        expected=[managed()],
        observed_at_ms=t,
        now_ms=t+500 if now is None else now,
        positions_complete=True,
        stops_complete=True,
    )
    payload.update(changes)
    return ShadowObservation(**payload)


class SessionTests(unittest.TestCase):
    def test_initial_healthy(self):
        s = ShadowSession()
        report = s.observe(observation())
        self.assertEqual(report.mode, "SHADOW_ONLY")
        self.assertEqual(report.observations, 1)
        self.assertFalse(s.halted)

    def test_multiple_healthy(self):
        s = ShadowSession()
        s.observe(observation())
        r = s.observe(observation(2000))
        self.assertEqual(r.observations, 2)

    def test_flat_account_is_safe_shadow_only(self):
        s = ShadowSession()
        r = s.observe(observation(positions=[], stops=[], expected=[]))
        self.assertEqual(r.last_result.position_count, 0)

    def test_duplicate_timestamp_permanently_halts(self):
        s = ShadowSession()
        s.observe(observation())
        with self.assertRaisesRegex(ShadowSessionHalted, "SHADOW_AUDIT_FAILED"):
            s.observe(observation())
        self.assertTrue(s.halted)
        with self.assertRaisesRegex(ShadowSessionHalted, "PERMANENTLY_HALTED"):
            s.observe(observation(2000))

    def test_backward_timestamp_halts(self):
        s = ShadowSession()
        s.observe(observation(2000))
        with self.assertRaises(ShadowSessionHalted):
            s.observe(observation(1000))
        self.assertTrue(s.halted)

    def test_large_gap_halts(self):
        s = ShadowSession(max_gap_ms=1000)
        s.observe(observation(1000))
        with self.assertRaises(ShadowSessionHalted):
            s.observe(observation(2001))
        self.assertTrue(s.halted)

    def test_exact_gap_boundary_passes(self):
        s = ShadowSession(max_gap_ms=1000)
        s.observe(observation(1000))
        self.assertEqual(s.observe(observation(2000)).observations, 2)

    def test_unexpected_stop_halts(self):
        s = ShadowSession()
        bad = observation(stops=[dict(positionId="10", id="other", state=1,
                                     stopLossPrice="175", vol="20")])
        with self.assertRaises(ShadowSessionHalted):
            s.observe(bad)
        self.assertTrue(s.halted)

    def test_incomplete_inventory_halts(self):
        s = ShadowSession()
        with self.assertRaises(ShadowSessionHalted):
            s.observe(observation(stops_complete=False))
        self.assertTrue(s.halted)

    def test_stale_inventory_halts(self):
        s = ShadowSession()
        with self.assertRaises(ShadowSessionHalted):
            s.observe(observation(now=4000))
        self.assertTrue(s.halted)

    def test_invalid_gap_config(self):
        for value in (0, -1, 300001, True, 1.5):
            with self.assertRaises(ValueError):
                ShadowSession(max_gap_ms=value)

    def test_trailing_proposal_is_only_data(self):
        s = ShadowSession()
        r = s.observe(observation(observed_lows={"SIREN_USDT": D("86")},
                                  price_ticks={"SIREN_USDT": D("0.01")}))
        self.assertEqual(r.last_result.proposals[0][2], D("86.50"))
        self.assertEqual(r.mode, "SHADOW_ONLY")
