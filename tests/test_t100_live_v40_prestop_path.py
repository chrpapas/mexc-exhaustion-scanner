"""Pure offline stepwise V40 prestop cross-equity safety regression tests."""
from __future__ import annotations

import unittest
from dataclasses import replace
from decimal import Decimal as D

from app.t100_live_v40_prestop_path import observe_v40_prestop_model_path
from tests.test_t100_live_cross_margin_stress import account, shorts


class V40PreStopPathTests(unittest.TestCase):
    def test_four_full_notional_shorts_can_breach_before_stop_reference(self):
        r = observe_v40_prestop_model_path(
            account=account(), positions=shorts(),
        )
        self.assertEqual(
            r.status,
            "V40_PRESTOP_MODELED_THRESHOLD_BREACH_AT_OR_BEFORE_FIRST_STOP",
        )
        self.assertEqual(r.modeled_first_stop_reference_pct, D("75"))
        self.assertIsNotNone(r.modeled_first_threshold_breach_pct)
        self.assertLess(r.modeled_first_threshold_breach_pct, D("75"))
        self.assertGreater(r.evaluated_shock_points, 1)
        self.assertFalse(r.model_no_threshold_breach_on_sampled_path)
        self.assertFalse(r.liquidation_path_certified_by_venue)
        self.assertFalse(r.verified_future_stressed_tiers)
        self.assertFalse(r.entry_allowed)
        self.assertFalse(r.live_authorization)
        self.assertFalse(r.mexc_orders_sent)

    def test_tiny_one_short_can_avoid_breach_in_hypothetical_model_only(self):
        small = replace(shorts(n=1)[0], contracts=D("100"))
        r = observe_v40_prestop_model_path(
            account=account(), positions=(small,),
        )
        self.assertEqual(
            r.status, "V40_PRESTOP_SAMPLED_PATH_NO_MODEL_BREACH_NONAUTHORIZING",
        )
        self.assertTrue(r.model_no_threshold_breach_on_sampled_path)
        self.assertEqual(r.evaluated_shock_points, 75)
        self.assertEqual(r.last_evaluated_shock_pct, D("75"))
        self.assertFalse(r.verified_future_stressed_tiers)
        self.assertFalse(r.account_wide_cross_collateral_certified)
        self.assertFalse(r.stop_execution_certified)
        self.assertFalse(r.live_authorization)

    def test_start_with_insufficient_initial_margin_halts(self):
        r = observe_v40_prestop_model_path(
            account=account(wallet_balance_usdt=D("100")),
            positions=shorts(),
        )
        self.assertEqual(r.status, "V40_PRESTOP_INITIAL_MARGIN_INFEASIBLE")
        self.assertFalse(r.model_no_threshold_breach_on_sampled_path)

    def test_reject_unverified_stressed_tiers_or_private_cross(self):
        p = replace(shorts(n=1)[0],
                    stressed_tier_upper_bound_verified=False)
        r = observe_v40_prestop_model_path(
            account=account(), positions=[p],
        )
        self.assertEqual(r.status, "V40_PRESTOP_EXCHANGE_EVIDENCE_INCOMPLETE")
        self.assertEqual(r.evaluated_shock_points, 0)
        p = replace(p, stressed_tier_upper_bound_verified=True,
                    cross_margin_verified=False)
        r = observe_v40_prestop_model_path(
            account=account(), positions=[p],
        )
        self.assertEqual(r.status, "V40_PRESTOP_EXCHANGE_EVIDENCE_INCOMPLETE")

    def test_current_venue_tier_cap_breaks_before_stop(self):
        p = replace(shorts(n=1)[0], contracts=D("100"),
                    tier_max_position_notional_usdt=D("103"))
        r = observe_v40_prestop_model_path(
            account=account(), positions=[p],
        )
        self.assertEqual(
            r.status, "V40_PRESTOP_MODEL_OR_TIER_BECAME_UNVERIFIED",
        )
        self.assertLess(r.last_evaluated_shock_pct, D("75"))
        self.assertFalse(r.model_no_threshold_breach_on_sampled_path)

    def test_first_stop_reference_of_two_is_evaluated_first(self):
        p1, p2 = shorts(n=2)
        earlier = replace(p1, current_fair_price=D("1.5"),
                          contracts=D("100"))
        later = replace(p2, contracts=D("100"))
        r = observe_v40_prestop_model_path(
            account=account(), positions=[earlier, later],
        )
        expected = (D("1.75") / D("1.5") - 1) * D(100)
        self.assertEqual(r.modeled_first_stop_reference_pct, expected)
        self.assertEqual(r.last_evaluated_shock_pct, expected)
        self.assertFalse(r.live_authorization)

    def test_reference_already_reached_is_not_swept(self):
        p = replace(shorts(n=1)[0], current_fair_price=D("1.75"))
        r = observe_v40_prestop_model_path(
            account=account(), positions=[p],
        )
        self.assertEqual(r.status, "V40_PRESTOP_STOP_REFERENCE_ALREADY_REACHED")
        self.assertEqual(r.evaluated_shock_points, 0)

    def test_stop_reference_above_supported_price_range_fails(self):
        p = replace(shorts(n=1)[0], current_fair_price=D("0.5"))
        r = observe_v40_prestop_model_path(
            account=account(), positions=[p],
        )
        self.assertEqual(r.status, "V40_PRESTOP_OUTSIDE_MODEL_SHOCK_RANGE")

    def test_short_position_schema_and_duplicates_fail_closed(self):
        p = shorts(n=1)[0]
        self.assertEqual(
            observe_v40_prestop_model_path(
                account=account(), positions=[],
            ).status, "V40_PRESTOP_INPUT_INVALID",
        )
        self.assertEqual(
            observe_v40_prestop_model_path(
                account=account(), positions=[p, p],
            ).status, "V40_PRESTOP_DUPLICATE_SYMBOL",
        )

    def test_step_size_bounded_and_fractional_boundary_included(self):
        p = replace(shorts(n=1)[0], contracts=D("100"),
                    current_fair_price=D("1.2"))
        r = observe_v40_prestop_model_path(
            account=account(), positions=[p], sample_step_pct=D("2.5"),
        )
        self.assertEqual(r.status,
                         "V40_PRESTOP_SAMPLED_PATH_NO_MODEL_BREACH_NONAUTHORIZING")
        self.assertEqual(r.last_evaluated_shock_pct,
                         (D("1.75") / D("1.2") - 1) * D(100))
        for bad in ("0", "0.49", "5.01", "NaN", True):
            with self.subTest(bad=bad):
                r = observe_v40_prestop_model_path(
                    account=account(), positions=[p], sample_step_pct=bad,
                )
                self.assertEqual(r.status, "V40_PRESTOP_INPUT_INVALID")

    def test_no_private_state_leaks_into_observation(self):
        p = replace(shorts(n=1)[0], symbol="SECRET_USDT",
                    contracts=D("101.12345"))
        r = observe_v40_prestop_model_path(
            account=account(), positions=[p],
        )
        self.assertNotIn("SECRET_USDT", str(r))
        self.assertNotIn("101.12345", str(r))

    def test_is_never_coupled_to_live_mexc_adapter(self):
        import inspect
        import app.t100_live_v40_prestop_path as source
        text = inspect.getsource(source)
        for forbidden in ("MexcTradeClient", "._private_post(",
                          "LiveMutationPermit", "httpx.", "asyncpg.",
                          ".send_one(", ".submit_short_with_stop("):
            self.assertNotIn(forbidden, text)


if __name__ == "__main__":
    unittest.main()
