"""Offline V40 SL75 + gap stress tests; absolutely no venue/API/orders."""
from __future__ import annotations

import unittest
from dataclasses import replace
from decimal import Decimal as D

from app.t100_live_v40_cross_stop_envelope import (
    assess_v40_cross_stop_envelope,
)
from tests.test_t100_live_cross_margin_stress import account, shorts


class V40StopEnvelopeTests(unittest.TestCase):
    def test_generic_twenty_percent_survival_does_not_certify_sl75(self):
        result = assess_v40_cross_stop_envelope(
            account=account(), positions=shorts(),
            adverse_fair_price_pct=D("0.20"),
        )
        self.assertEqual(result.status, "V40_CROSS_STRESS_BELOW_STOP_GAP_FLOOR")
        self.assertEqual(result.required_adverse_fair_pct, D("80.00"))
        self.assertFalse(result.scenario_stress_survived)
        self.assertFalse(result.entry_allowed)
        self.assertFalse(result.live_authorization)

    def test_default_floors_at_75_percent_stop_plus_5_percent_gap(self):
        result = assess_v40_cross_stop_envelope(
            account=account(), positions=shorts(),
        )
        self.assertEqual(result.scenario_adverse_fair_pct, D("80.00"))
        self.assertEqual(result.required_adverse_fair_pct, D("80.00"))
        self.assertEqual(result.status, "V40_STOP_GAP_SCENARIO_BREACH_NONAUTHORIZING")
        self.assertFalse(result.scenario_stress_survived)
        self.assertFalse(result.stressed_tiers_certified)
        self.assertFalse(result.account_collateral_certified)
        self.assertFalse(result.venue_liquidation_certified)
        self.assertFalse(result.mexc_orders_sent)

    def test_small_single_short_can_survive_hypothetical_no_promotion(self):
        one = replace(shorts(n=1)[0], contracts=D("100"))
        result = assess_v40_cross_stop_envelope(
            account=account(), positions=[one],
            adverse_fair_price_pct=D("0.90"),
        )
        self.assertEqual(result.status, "V40_STOP_GAP_SCENARIO_SURVIVES_NONAUTHORIZING")
        self.assertTrue(result.scenario_stress_survived)
        self.assertFalse(result.live_authorization)
        self.assertFalse(result.venue_liquidation_certified)
        self.assertFalse(result.account_collateral_certified)

    def test_overshoot_buffer_at_least_five_percent(self):
        for value in (D(0), D("0.01"), D("0.049"), D("NaN")):
            with self.subTest(value=value):
                result = assess_v40_cross_stop_envelope(
                    account=account(), positions=shorts(),
                    additional_gap_pct_of_current=value,
                )
                self.assertIn(result.status, {
                    "V40_CROSS_STRESS_GAP_BUFFER_TOO_SMALL_OR_INVALID",
                    "V40_CROSS_STRESS_INPUT_INVALID",
                })
                self.assertFalse(result.live_authorization)

    def test_current_below_entry_needs_larger_shock(self):
        low = replace(shorts(n=1)[0], current_fair_price=D("0.75"))
        r = assess_v40_cross_stop_envelope(
            account=account(), positions=[low],
        )
        expected = ((D("1.75") / D("0.75")) - 1 + D("0.05")) * 100
        self.assertEqual(r.required_adverse_fair_pct, expected)
        self.assertNotEqual(r.required_adverse_fair_pct, D("80"))

    def test_already_above_stop_does_not_get_green_scenario(self):
        high = replace(shorts(n=1)[0], current_fair_price=D("1.80"))
        r = assess_v40_cross_stop_envelope(
            account=account(), positions=[high],
        )
        self.assertEqual(r.status, "V40_CROSS_STRESS_STOP_REFERENCE_ALREADY_REACHED")
        self.assertFalse(r.scenario_stress_survived)

    def test_deep_downmove_stop_floor_exceeds_model_range(self):
        low = replace(shorts(n=1)[0], current_fair_price=D("0.5"))
        r = assess_v40_cross_stop_envelope(
            account=account(), positions=[low],
        )
        self.assertEqual(r.status, "V40_CROSS_STRESS_FLOOR_OUTSIDE_MODEL_RANGE")

    def test_unverified_stressed_tier_or_cross_mode_blocks_even_small_size(self):
        p = replace(shorts(n=1)[0], contracts=D("100"),
                    stressed_tier_upper_bound_verified=False)
        r = assess_v40_cross_stop_envelope(account=account(), positions=[p])
        self.assertEqual(r.status, "V40_CROSS_STRESS_EXCHANGE_EVIDENCE_INCOMPLETE")
        p = replace(p, stressed_tier_upper_bound_verified=True,
                    cross_margin_verified=False)
        r = assess_v40_cross_stop_envelope(account=account(), positions=[p])
        self.assertEqual(r.status, "V40_CROSS_STRESS_EXCHANGE_EVIDENCE_INCOMPLETE")

    def test_pending_order_reservation_preserved_in_scenario(self):
        small = replace(shorts(n=1)[0], contracts=D("100"))
        thin = account(wallet_balance_usdt=D("500"),
                       pending_order_margin_usdt=D("450"))
        result = assess_v40_cross_stop_envelope(
            account=thin, positions=[small],
        )
        self.assertEqual(result.status, "V40_STOP_GAP_SCENARIO_BREACH_NONAUTHORIZING")

    def test_gap_stronger_than_default_meets_floor(self):
        r = assess_v40_cross_stop_envelope(
            account=account(), positions=shorts(),
            additional_gap_pct_of_current=D("0.25"),
            adverse_fair_price_pct=D("1"),
        )
        self.assertEqual(r.required_adverse_fair_pct, D("100.00"))
        self.assertEqual(r.scenario_adverse_fair_pct, D("100"))

    def test_invalid_type_and_duplicate_positions_fail_closed(self):
        self.assertEqual(
            assess_v40_cross_stop_envelope(account=account(), positions=[]).status,
            "V40_CROSS_STRESS_INPUT_INVALID",
        )
        p1, p2 = shorts(n=2)
        duplicated = [p1, replace(p2, symbol=p1.symbol)]
        r = assess_v40_cross_stop_envelope(account=account(), positions=duplicated)
        self.assertEqual(r.status, "V40_CROSS_STRESS_EXCHANGE_EVIDENCE_INCOMPLETE")
        self.assertFalse(r.live_authorization)


if __name__ == "__main__":
    unittest.main()
