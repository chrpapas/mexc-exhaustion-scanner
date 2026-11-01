"""Offline V40 modeled-notional capacity/parity decision regressions.

Tests never read account data or authorize execution. Public contract
capacity is not historical V40 return parity or a live order permit.
"""
from __future__ import annotations

from dataclasses import replace
from decimal import Decimal as D
import unittest

from app.t100_live_public_v40_capacity import assess_public_v40_capacity
from app.t100_live_v40_capacity_parity import assess_v40_public_capacity_parity
from app.t100_live_pilot_sizing import plan_tiny_pilot_notional
from tests.test_t100_live_public_v40_capacity import siren
from tests.test_t100_v40_admission import run, prior


class V40CapacityParityTests(unittest.TestCase):
    def test_frozen_high_slot_6250_rejected_by_siren_at_reference_price(self):
        admission = run(bars=prior(1e9))
        self.assertEqual(admission.action, "ADMIT")
        self.assertEqual(admission.desired_notional_usdt, 6250.0)
        self.assertEqual(admission.modeled_notional_usdt, 6250.0)
        capacity = assess_public_v40_capacity(
            siren(), desired_notional_usdt=admission.modeled_notional_usdt,
            entry_reference_price="0.02298",
        )
        result = assess_v40_public_capacity_parity(admission, capacity)
        self.assertEqual(result.status,
                         "V40_CAPACITY_PARITY_PUBLISHED_POSITION_CAP_REJECT")
        self.assertFalse(result.published_full_modeled_notional_fits)
        self.assertGreater(result.published_capacity_shortfall_usdt, D("5000"))
        self.assertLess(result.floor_quantization_remainder_usdt, D("0.2298"))
        self.assertEqual(result.quoted_public_capacity_usdt, D("827.28000"))
        self.assertFalse(result.historical_v40_quantity_parity_certified)
        self.assertFalse(result.live_order_submission_allowed)
        self.assertFalse(result.live_authorization)

    def test_exact_modeled_target_can_fit_public_but_never_authorizes(self):
        admission = replace(
            run(bars=prior(1e9)), modeled_notional_usdt=500.0,
        )
        capacity = assess_public_v40_capacity(
            siren(), desired_notional_usdt=500,
            entry_reference_price="0.02298",
        )
        report = assess_v40_public_capacity_parity(admission, capacity)
        self.assertEqual(report.status,
                         "V40_CAPACITY_PARITY_PUBLIC_QUANTITY_FITS_NONAUTHORIZING")
        self.assertTrue(report.published_full_modeled_notional_fits)
        self.assertTrue(report.published_position_tier_fits)
        self.assertTrue(report.published_single_order_fits)
        self.assertFalse(report.historical_v40_quantity_parity_certified)
        self.assertFalse(report.liquidity_and_stop_exit_verified)
        self.assertFalse(report.private_cross_margin_verified)
        self.assertFalse(report.live_order_submission_allowed)

    def test_reject_incorrect_high_tier_target_instead_of_modeled_amount(self):
        admission = replace(
            run(bars=prior(1e9)), modeled_notional_usdt=500.0,
        )
        wrong = assess_public_v40_capacity(
            siren(), desired_notional_usdt=6250,
            entry_reference_price="0.02298",
        )
        report = assess_v40_public_capacity_parity(admission, wrong)
        self.assertEqual(report.status, "V40_CAPACITY_PARITY_TARGET_MISMATCH")

    def test_single_order_excess_is_distinct_from_position_cap_excess(self):
        row = siren()
        row["maxVol"] = 500
        admission = replace(
            run(bars=prior(1e9)), modeled_notional_usdt=200.0,
        )
        capacity = assess_public_v40_capacity(
            row, desired_notional_usdt=200, entry_reference_price="0.02298",
        )
        report = assess_v40_public_capacity_parity(admission, capacity)
        self.assertEqual(
            report.status, "V40_CAPACITY_PARITY_SINGLE_ORDER_CAP_REJECT",
        )
        self.assertTrue(report.published_position_tier_fits)
        self.assertFalse(report.published_single_order_fits)
        self.assertGreater(report.published_capacity_shortfall_usdt, D(0))
        self.assertFalse(report.live_authorization)

    def test_refuse_pilot_size_as_replacement_for_frozen_v40_size(self):
        original = run(bars=prior(1e9))
        pilot = plan_tiny_pilot_notional(
            original, equity_usdt=10000, explicit_pilot_cap_usdt=500,
        )
        self.assertFalse(pilot.historical_notional_parity)
        capacity = assess_public_v40_capacity(
            siren(), desired_notional_usdt=pilot.pilot_notional_usdt,
            entry_reference_price="0.02298",
        )
        result = assess_v40_public_capacity_parity(original, capacity)
        self.assertEqual(result.status, "V40_CAPACITY_PARITY_TARGET_MISMATCH")
        self.assertFalse(result.historical_v40_quantity_parity_certified)

    def test_rejected_admission_does_not_claim_capacity(self):
        x = replace(run(bars=prior(1e9)), action="REJECT")
        cap = assess_public_v40_capacity(
            siren(), desired_notional_usdt=500, entry_reference_price="0.02298",
        )
        result = assess_v40_public_capacity_parity(x, cap)
        self.assertEqual(result.status, "V40_CAPACITY_PARITY_NOT_AN_ADMITTED_MODEL")

    def test_unverifiable_public_status_cannot_be_coerced_into_positive(self):
        admission = replace(
            run(bars=prior(1e9)), modeled_notional_usdt=500.0,
        )
        cap = assess_public_v40_capacity(
            siren(), desired_notional_usdt=500, entry_reference_price="0.02298",
        )
        broken = replace(
            cap, status="SOME_OTHER_STATUS",
        )
        result = assess_v40_public_capacity_parity(admission, broken)
        self.assertEqual(result.status, "V40_CAPACITY_PARITY_PUBLIC_OUTCOME_UNVERIFIED")
        self.assertFalse(result.published_full_modeled_notional_fits)

    def test_missing_or_inconsistent_model_inputs_fail_closed(self):
        a = run(bars=prior(1e9))
        cap = assess_public_v40_capacity(
            siren(), desired_notional_usdt=6250,
            entry_reference_price="0.02298",
        )
        for model in (
            None, "ADMIT", replace(a, modeled_notional_usdt=0.0),
            replace(a, modeled_notional_usdt=float("nan")),
            replace(a, modeled_notional_usdt=100000.0),
        ):
            with self.subTest(model=model):
                result = assess_v40_public_capacity_parity(model, cap)
                self.assertFalse(result.published_full_modeled_notional_fits)
                self.assertFalse(result.live_authorization)

    def test_no_order_transport_or_private_account_dependency(self):
        import inspect
        import app.t100_live_v40_capacity_parity as module
        text = inspect.getsource(module)
        for forbidden in (
            "._private_post(", ".send_one(", "MexcTradeClient(",
            "httpx.", "asyncpg.", "LiveMutationPermit",
        ):
            self.assertNotIn(forbidden, text)


if __name__ == "__main__":
    unittest.main()
