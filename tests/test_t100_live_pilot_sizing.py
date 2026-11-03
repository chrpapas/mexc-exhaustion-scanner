"""Offline tests for explicit pilot-vs-parity sizing boundary."""
import unittest
from decimal import Decimal as D

from app.t100_live_pilot_sizing import (
    PilotSizingHalted,
    plan_tiny_pilot_notional,
)
from app.t100_v40_admission import V40Admission


def admitted(modeled=500):
    return V40Admission(
        action="ADMIT",
        reason="modeled_single_bar_admission",
        modeled_notional_usdt=modeled,
    )


class PilotSizingTests(unittest.TestCase):
    def test_downsized_pilot_is_explicitly_nonparity(self):
        p = plan_tiny_pilot_notional(
            admitted(500),
            equity_usdt=2000,
            explicit_pilot_cap_usdt=100,
        )
        self.assertEqual(p.pilot_notional_usdt, D("100"))
        self.assertFalse(p.historical_notional_parity)
        self.assertEqual(
            p.purpose, "EXECUTION_PILOT_DOWNSIZED_NONPARITY"
        )
        self.assertFalse(p.live_authorization)

    def test_equity_fraction_also_caps_pilot(self):
        p = plan_tiny_pilot_notional(
            admitted(1000),
            equity_usdt=2000,
            explicit_pilot_cap_usdt=1000,
        )
        self.assertEqual(p.pilot_notional_usdt, D("500"))
        self.assertFalse(p.historical_notional_parity)

    def test_parity_true_only_when_nothing_downsizes(self):
        p = plan_tiny_pilot_notional(
            admitted(100),
            equity_usdt=2000,
            explicit_pilot_cap_usdt=200,
        )
        self.assertEqual(p.pilot_notional_usdt, D("100"))
        self.assertTrue(p.historical_notional_parity)
        self.assertEqual(p.purpose, "V40_NOTIONAL_PARITY")

    def test_non_admitted_signal_never_gets_pilot_notional(self):
        for action in ("REJECT", "SKIP", "MISSING"):
            with self.subTest(action=action):
                with self.assertRaisesRegex(
                    PilotSizingHalted, "V40_NOT_ADMITTED"
                ):
                    plan_tiny_pilot_notional(
                        V40Admission(action=action, reason="x"),
                        equity_usdt=2000,
                        explicit_pilot_cap_usdt=100,
                    )

    def test_invalid_cap_or_fraction_blocks(self):
        with self.assertRaises(PilotSizingHalted):
            plan_tiny_pilot_notional(
                admitted(), equity_usdt=2000,
                explicit_pilot_cap_usdt=0,
            )
        with self.assertRaises(PilotSizingHalted):
            plan_tiny_pilot_notional(
                admitted(), equity_usdt=2000,
                explicit_pilot_cap_usdt=100,
                max_equity_fraction=D("1.1"),
            )


if __name__ == "__main__":
    unittest.main()
