from __future__ import annotations

import unittest
from decimal import Decimal

from app.t100_live_guards import (
    LIVE_ADAPTER_STATUS,
    LIVE_CONFIRMATION,
    T100ExecutionControls,
    require_paper_portfolio,
)


class T100LiveGuardTests(unittest.TestCase):
    def test_default_is_paper(self):
        controls = T100ExecutionControls.from_env({})
        self.assertEqual(controls.mode, "paper")
        require_paper_portfolio(controls)

    def test_live_always_fails_even_if_operator_flags_are_all_set(self):
        env = {
            "T100_EXECUTION_MODE": "live",
            "T100_LIVE_ORDER_API_ENABLED": "true",
            "T100_LIVE_KILL_SWITCH": "false",
            "T100_LIVE_OPERATOR_CONFIRM": LIVE_CONFIRMATION,
            "T100_LIVE_MAX_POSITION_NOTIONAL_USDT": "100",
            "T100_LIVE_MAX_TOTAL_NOTIONAL_USDT": "200",
            "T100_LIVE_MAX_DAILY_LOSS_USDT": "10",
            "T100_LIVE_LEVERAGE": "1",
        }
        controls = T100ExecutionControls.from_env(env)
        self.assertEqual(controls.readiness_failures(), ())
        with self.assertRaisesRegex(RuntimeError, "T100_LIVE_EXECUTION_BLOCKED"):
            require_paper_portfolio(controls)
        self.assertEqual(LIVE_ADAPTER_STATUS, "NOT_IMPLEMENTED")

    def test_live_without_funding_risk_limits_fails(self):
        controls = T100ExecutionControls.from_env({"T100_EXECUTION_MODE": "live"})
        self.assertIn("missing_position_limit", controls.readiness_failures())
        self.assertIn("kill_switch_engaged", controls.readiness_failures())
        with self.assertRaisesRegex(RuntimeError, "T100_LIVE_EXECUTION_BLOCKED"):
            require_paper_portfolio(controls)

    def test_position_limit_cannot_exceed_portfolio_cap(self):
        controls = T100ExecutionControls.from_env({
            "T100_EXECUTION_MODE": "live",
            "T100_LIVE_MAX_POSITION_NOTIONAL_USDT": "200",
            "T100_LIVE_MAX_TOTAL_NOTIONAL_USDT": "100",
        })
        self.assertIn("position_limit_exceeds_portfolio_limit", controls.readiness_failures())

    def test_no_invalid_modes(self):
        for mode in ("", "LIVE_NOW", "real", "auto"):
            with self.subTest(mode=mode), self.assertRaises(ValueError):
                T100ExecutionControls.from_env({"T100_EXECUTION_MODE": mode})

    def test_leverage_bounds_fail_closed(self):
        for leverage in ("0", "3", "-1", "1.5", "oops"):
            with self.subTest(leverage=leverage), self.assertRaises(ValueError):
                T100ExecutionControls.from_env({"T100_LIVE_LEVERAGE": leverage})

    def test_invalid_limits_fail_closed(self):
        for limit in ("-1", "0", "NaN", "Infinity", "invalid"):
            with self.subTest(limit=limit), self.assertRaises(ValueError):
                T100ExecutionControls.from_env({
                    "T100_LIVE_MAX_POSITION_NOTIONAL_USDT": limit
                })

    def test_strict_bool_parser(self):
        with self.assertRaises(ValueError):
            T100ExecutionControls.from_env({
                "T100_LIVE_ORDER_API_ENABLED": "maybe"
            })

    def test_paper_remains_paper_even_when_flags_set(self):
        controls = T100ExecutionControls.from_env({
            "T100_EXECUTION_MODE": "paper",
            "T100_LIVE_ORDER_API_ENABLED": "true",
            "T100_LIVE_KILL_SWITCH": "false",
        })
        require_paper_portfolio(controls)

    def test_accepted_decimal_limits_are_exact(self):
        c = T100ExecutionControls.from_env({
            "T100_EXECUTION_MODE": "live",
            "T100_LIVE_MAX_POSITION_NOTIONAL_USDT": "12.3456",
            "T100_LIVE_MAX_TOTAL_NOTIONAL_USDT": "20",
        })
        self.assertEqual(c.max_position_notional_usdt, Decimal("12.3456"))

if __name__ == "__main__":
    unittest.main()
