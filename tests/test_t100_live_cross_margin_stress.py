"""Offline MEXC cross-margin shock-model tests; no private API or orders."""
from __future__ import annotations

import unittest
from dataclasses import replace
from decimal import Decimal as D

from app.t100_live_cross_margin_stress import (
    CrossAccount, ShortCrossPosition, assess_cross_margin_shock,
)


def account(**changes):
    a = CrossAccount(
        wallet_balance_usdt=D("2000"),
        isolated_position_margin_usdt=D("0"),
        pending_order_margin_usdt=D("0"),
        committed_cash_reserve_usdt=D("100"),
        entry_and_future_fees_reserve_usdt=D("10"),
        additional_adverse_cashflow_usdt=D("10"),
        account_inventory_complete_verified=True,
        usdt_wallet_economic_witness_verified=True,
        risk_limits_fresh_verified=True,
    )
    return replace(a, **changes)


def shorts(*, n=4, leverage=5, mmr="0.02", fee="0.005"):
    # Fictional contract parameters; NOT a claim about actual MEXC tiers.
    return tuple(
        ShortCrossPosition(
            symbol=f"TEST{i}_USDT",
            entry_fair_price=D("1"),
            current_fair_price=D("1"),
            contracts=D("1250"),
            contract_size=D("1"),
            configured_leverage=leverage,
            tier_max_position_notional_usdt=D("5000"),
            worst_stress_mmr=D(mmr),
            liquidation_fee_rate=D(fee),
            stressed_tier_upper_bound_verified=True,
            cross_margin_verified=True,
            fair_price_verified=True,
        )
        for i in range(n)
    )


class CrossMarginShockTests(unittest.TestCase):
    def test_four_high_slots_shock_survives_with_five_x_model(self):
        report = assess_cross_margin_shock(
            account=account(), positions=shorts(),
        )
        self.assertEqual(
            report.status, "CROSS_STRESS_SCENARIO_SURVIVES_NONAUTHORIZING",
        )
        self.assertEqual(report.gross_entry_notional_usdt, D("5000"))
        self.assertEqual(report.configured_initial_margin_usdt, D("1000"))
        self.assertEqual(report.gross_stressed_notional_usdt, D("6000"))
        self.assertEqual(report.stressed_unrealized_pnl_usdt, D("-1000"))
        self.assertEqual(report.stressed_cross_equity_usdt, D("980"))
        self.assertEqual(report.stressed_maintenance_margin_usdt, D("120"))
        self.assertEqual(report.stressed_liquidation_fee_usdt, D("30"))
        self.assertTrue(report.modeled_stress_survived)
        self.assertGreater(report.coverage_ratio, D("6"))
        self.assertFalse(report.venue_liquidation_certified)
        self.assertFalse(report.entry_allowed)
        self.assertFalse(report.live_authorization)

    def test_same_gross_exposure_is_infeasible_at_one_x(self):
        result = assess_cross_margin_shock(
            account=account(), positions=shorts(leverage=1),
        )
        self.assertEqual(
            result.status,
            "CROSS_STRESS_INITIAL_MARGIN_INFEASIBLE_NONAUTHORIZING",
        )
        self.assertEqual(result.configured_initial_margin_usdt, D("5000"))
        self.assertFalse(result.modeled_stress_survived)

    def test_extreme_shock_can_breach_maintenance_threshold(self):
        result = assess_cross_margin_shock(
            account=account(),
            positions=shorts(),
            adverse_fair_price_pct=D("0.50"),
        )
        self.assertEqual(
            result.status,
            "CROSS_STRESS_LIQUIDATION_THRESHOLD_BREACH_NONAUTHORIZING",
        )
        self.assertLess(result.stressed_cross_equity_usdt, D(0))
        self.assertFalse(result.modeled_stress_survived)

    def test_missing_tier_evidence_blocks(self):
        positions = list(shorts())
        positions[2] = replace(
            positions[2], stressed_tier_upper_bound_verified=False,
        )
        report = assess_cross_margin_shock(
            account=account(), positions=positions,
        )
        self.assertEqual(
            report.status, "CROSS_STRESS_EXCHANGE_EVIDENCE_INCOMPLETE",
        )

    def test_shock_notional_outgrows_verified_tier_cap(self):
        positions = list(shorts())
        positions[0] = replace(
            positions[0], tier_max_position_notional_usdt=D("1350"),
        )
        r = assess_cross_margin_shock(
            account=account(), positions=positions,
        )
        self.assertEqual(
            r.status, "CROSS_STRESS_RISK_TIER_CAP_NOT_VERIFIED",
        )

    def test_maintenance_margin_is_conservatively_tier_bound(self):
        r = assess_cross_margin_shock(
            account=account(),
            positions=shorts(mmr="0.25"),
        )
        self.assertEqual(r.stressed_maintenance_margin_usdt, D("1500"))
        self.assertFalse(r.modeled_stress_survived)

    def test_unverified_mode_or_fair_price_blocks(self):
        positions = list(shorts())
        positions[0] = replace(positions[0], cross_margin_verified=False)
        self.assertEqual(
            assess_cross_margin_shock(
                account=account(), positions=positions,
            ).status, "CROSS_STRESS_EXCHANGE_EVIDENCE_INCOMPLETE",
        )
        positions[0] = replace(
            positions[0], cross_margin_verified=True,
            fair_price_verified=False,
        )
        self.assertEqual(
            assess_cross_margin_shock(
                account=account(), positions=positions,
            ).status, "CROSS_STRESS_EXCHANGE_EVIDENCE_INCOMPLETE",
        )

    def test_missing_account_economic_witness_blocks(self):
        r = assess_cross_margin_shock(
            account=account(usdt_wallet_economic_witness_verified=False),
            positions=shorts(),
        )
        self.assertEqual(
            r.status, "CROSS_STRESS_EXCHANGE_EVIDENCE_INCOMPLETE",
        )

    def test_unsettled_funding_or_fee_reserve_reduces_coverage(self):
        baseline = assess_cross_margin_shock(
            account=account(), positions=shorts(),
        )
        worse = assess_cross_margin_shock(
            account=account(
                additional_adverse_cashflow_usdt=D("80"),
                entry_and_future_fees_reserve_usdt=D("40"),
            ),
            positions=shorts(),
        )
        self.assertLess(worse.stressed_cross_equity_usdt,
                        baseline.stressed_cross_equity_usdt)
        self.assertLess(worse.coverage_ratio, baseline.coverage_ratio)

    def test_isolated_margin_and_order_reservations_reduce_buffer(self):
        base = assess_cross_margin_shock(
            account=account(), positions=shorts(),
        )
        altered = assess_cross_margin_shock(
            account=account(
                isolated_position_margin_usdt=D("200"),
                pending_order_margin_usdt=D("100"),
            ), positions=shorts(),
        )
        self.assertEqual(
            altered.stressed_cross_equity_usdt,
            base.stressed_cross_equity_usdt - D("300"),
        )
        self.assertLess(
            altered.available_initial_margin_usdt,
            base.available_initial_margin_usdt,
        )

    def test_current_fair_price_differs_from_entry(self):
        positions = list(shorts())
        positions[0] = replace(
            positions[0], current_fair_price=D("1.1"),
        )
        altered = assess_cross_margin_shock(
            account=account(), positions=positions,
        )
        self.assertLess(
            altered.stressed_cross_equity_usdt,
            assess_cross_margin_shock(
                account=account(), positions=shorts(),
            ).stressed_cross_equity_usdt,
        )

    def test_duplicate_or_fifth_slot_never_passes(self):
        positions = list(shorts())
        positions[1] = replace(positions[1], symbol=positions[0].symbol)
        self.assertEqual(
            assess_cross_margin_shock(
                account=account(), positions=positions,
            ).status, "CROSS_STRESS_DUPLICATE_SYMBOL",
        )
        self.assertEqual(
            assess_cross_margin_shock(
                account=account(), positions=shorts(n=5),
            ).status, "CROSS_STRESS_ONE_TO_FOUR_SHORTS_REQUIRED",
        )

    def test_invalid_data_and_nan_block(self):
        cases = (
            (account(wallet_balance_usdt=D("NaN")), shorts()),
            (account(risk_limits_fresh_verified=None), shorts()),
            (account(), shorts(mmr="-1")),
            (account(), shorts(leverage=0)),
            (account(), shorts(fee="Infinity")),
        )
        for a, ps in cases:
            with self.subTest(account=a, positions=ps):
                r = assess_cross_margin_shock(account=a, positions=ps)
                self.assertEqual(r.status, "CROSS_STRESS_INPUT_INVALID")
                self.assertFalse(r.live_authorization)

    def test_no_input_is_never_interpreted_as_safe(self):
        self.assertEqual(
            assess_cross_margin_shock(account=account(), positions=[]).status,
            "CROSS_STRESS_ONE_TO_FOUR_SHORTS_REQUIRED",
        )
        self.assertEqual(
            assess_cross_margin_shock(
                account=account(), positions=shorts(),
                adverse_fair_price_pct=D(0),
            ).status, "CROSS_STRESS_SHOCK_INVALID",
        )


if __name__ == "__main__":
    unittest.main()
