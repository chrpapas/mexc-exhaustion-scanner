"""Pure tests for one-way operational exposure/loss kill controls."""
import unittest
from decimal import Decimal as D

from app.t100_live_operational_risk import (
    OperationalRiskHalted,
    OperationalRiskLimits,
    OperationalRiskMonitor,
)


def limits():
    return OperationalRiskLimits(
        max_position_notional_usdt=D("600"),
        max_total_notional_usdt=D("2000"),
        max_daily_loss_usdt=D("100"),
    )


class OperationalRiskTests(unittest.TestCase):
    def test_healthy_snapshot_is_non_authorizing(self):
        m = OperationalRiskMonitor(limits())
        r = m.evaluate(
            position_notionals_usdt={"p1": D("500"), "p2": D("400")},
            total_notional_usdt=D("900"),
            daily_loss_usdt=D("20"),
            manual_kill_switch=False,
        )
        self.assertEqual(r.status, "WITHIN_LIMITS_NON_AUTHORIZING")
        self.assertFalse(r.kill_tripped)
        self.assertFalse(r.entry_allowed)
        self.assertFalse(r.live_authorization)

    def test_manual_kill_latches(self):
        m = OperationalRiskMonitor(limits())
        with self.assertRaisesRegex(
            OperationalRiskHalted, "MANUAL_KILL_SWITCH"):
            m.evaluate(
                position_notionals_usdt={},
                total_notional_usdt=0,
                daily_loss_usdt=0,
                manual_kill_switch=True,
            )
        self.assertEqual(m.kill.first_reason, "MANUAL_KILL_SWITCH")
        with self.assertRaisesRegex(Exception, "KILL_LATCHED"):
            m.evaluate(
                position_notionals_usdt={},
                total_notional_usdt=0,
                daily_loss_usdt=0,
                manual_kill_switch=False,
            )

    def test_position_limit_latches(self):
        m = OperationalRiskMonitor(limits())
        with self.assertRaisesRegex(
            OperationalRiskHalted, "POSITION_EXPOSURE_LIMIT"):
            m.evaluate(
                position_notionals_usdt={"p1": D("600.01")},
                total_notional_usdt=D("600.01"),
                daily_loss_usdt=0,
                manual_kill_switch=False,
            )
        self.assertEqual(m.kill.first_reason, "POSITION_EXPOSURE_LIMIT")

    def test_position_limit_boundary_passes(self):
        m = OperationalRiskMonitor(limits())
        r = m.evaluate(
            position_notionals_usdt={"p1": D("600")},
            total_notional_usdt=D("600"),
            daily_loss_usdt=0,
            manual_kill_switch=False,
        )
        self.assertTrue(r.within_position_limit)

    def test_portfolio_limit_latches(self):
        m = OperationalRiskMonitor(limits())
        with self.assertRaisesRegex(
            OperationalRiskHalted, "PORTFOLIO_EXPOSURE_LIMIT"):
            m.evaluate(
                position_notionals_usdt={
                    "p1": D("500"), "p2": D("500"),
                    "p3": D("500"), "p4": D("500.01"),
                },
                total_notional_usdt=D("2000.01"),
                daily_loss_usdt=0,
                manual_kill_switch=False,
            )
        self.assertEqual(m.kill.first_reason, "PORTFOLIO_EXPOSURE_LIMIT")

    def test_portfolio_limit_boundary_passes(self):
        m = OperationalRiskMonitor(limits())
        r = m.evaluate(
            position_notionals_usdt={
                "p1": D("500"), "p2": D("500"),
                "p3": D("500"), "p4": D("500"),
            },
            total_notional_usdt=D("2000"),
            daily_loss_usdt=0,
            manual_kill_switch=False,
        )
        self.assertTrue(r.within_portfolio_limit)

    def test_daily_loss_limit_latches_at_boundary(self):
        m = OperationalRiskMonitor(limits())
        with self.assertRaisesRegex(
            OperationalRiskHalted, "DAILY_LOSS_LIMIT"):
            m.evaluate(
                position_notionals_usdt={},
                total_notional_usdt=0,
                daily_loss_usdt=D("100"),
                manual_kill_switch=False,
            )
        self.assertEqual(m.kill.first_reason, "DAILY_LOSS_LIMIT")

    def test_daily_loss_below_boundary_passes(self):
        m = OperationalRiskMonitor(limits())
        r = m.evaluate(
            position_notionals_usdt={},
            total_notional_usdt=0,
            daily_loss_usdt=D("99.999"),
            manual_kill_switch=False,
        )
        self.assertTrue(r.within_daily_loss_limit)

    def test_total_notional_must_reconcile_to_positions(self):
        m = OperationalRiskMonitor(limits())
        with self.assertRaisesRegex(
            OperationalRiskHalted, "RECONCILIATION_DRIFT"):
            m.evaluate(
                position_notionals_usdt={"p1": D("500")},
                total_notional_usdt=D("499"),
                daily_loss_usdt=0,
                manual_kill_switch=False,
            )
        self.assertFalse(m.kill.tripped)

    def test_invalid_numeric_inputs_fail_without_silent_coercion(self):
        for bad in ("NaN", "Infinity", -1):
            m = OperationalRiskMonitor(limits())
            with self.assertRaises(OperationalRiskHalted):
                m.evaluate(
                    position_notionals_usdt={"p1": bad},
                    total_notional_usdt=bad,
                    daily_loss_usdt=0,
                    manual_kill_switch=False,
                )

    def test_invalid_manual_kill_type_rejected(self):
        m = OperationalRiskMonitor(limits())
        with self.assertRaisesRegex(
            OperationalRiskHalted, "INVALID_MANUAL_KILL_SWITCH"):
            m.evaluate(
                position_notionals_usdt={},
                total_notional_usdt=0,
                daily_loss_usdt=0,
                manual_kill_switch=1,
            )

    def test_invalid_limits_rejected(self):
        for bad in (D("0"), D("-1"), D("NaN")):
            with self.assertRaises(ValueError):
                OperationalRiskLimits(
                    max_position_notional_usdt=bad,
                    max_total_notional_usdt=D("2000"),
                    max_daily_loss_usdt=D("100"),
                )

    def test_position_limit_cannot_exceed_portfolio_limit(self):
        with self.assertRaisesRegex(
            ValueError, "POSITION_LIMIT_EXCEEDS_PORTFOLIO_LIMIT"):
            OperationalRiskLimits(
                max_position_notional_usdt=D("2001"),
                max_total_notional_usdt=D("2000"),
                max_daily_loss_usdt=D("100"),
            )

    def test_first_breach_reason_cannot_be_cleared(self):
        m = OperationalRiskMonitor(limits())
        with self.assertRaises(OperationalRiskHalted):
            m.evaluate(
                position_notionals_usdt={"p1": D("700")},
                total_notional_usdt=D("700"),
                daily_loss_usdt=0,
                manual_kill_switch=False,
            )
        r = m.report_after_kill()
        self.assertTrue(r.kill_tripped)
        self.assertEqual(r.kill_reason, "POSITION_EXPOSURE_LIMIT")
        self.assertFalse(r.entry_allowed)
        self.assertFalse(r.live_authorization)
