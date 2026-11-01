"""Read-only mixed account tier schema triage: no API calls or permits."""
from __future__ import annotations

import copy
import unittest
from dataclasses import replace

from app.t100_live_mixed_tier_shape import assess_mixed_tier_shape
from app.t100_live_mixed_margin_observation import (
    observe_mixed_margin_account_window,
)
from tests.test_t100_live_mixed_margin_observation import mixed, Reader


def mixed_with_private_sides():
    f = mixed()
    row = f.private_risk_by_symbol["SECOND_USDT"][0]
    row["positionType"] = 1
    row["openType"] = 1
    return f


def increment(f, symbol):
    pub = f.public_contract_by_symbol[symbol]
    pub.update(
        riskLimitMode="INCREASE",
        riskLimitType="BY_VOLUME",
        riskBaseVol=500, riskIncrVol=250, riskLevelLimit=4,
        riskIncrMmr="0.005", riskIncrImr="0.005",
        maintenanceMarginRate="0.01", initialMarginRate="0.02",
    )
    return f


class MixedTierShapeTests(unittest.IsolatedAsyncioTestCase):
    def classify(self, f):
        return assess_mixed_tier_shape(
            f.positions, f.private_risk_by_symbol,
            f.public_contract_by_symbol,
        )

    def test_current_private_rows_not_stressed_tier_certification(self):
        f = mixed_with_private_sides()
        out = self.classify(f)
        self.assertEqual(
            out.status,
            "MIXED_CUSTOM_VOLUME_TERMS_OBSERVED_NONAUTHORIZING",
        )
        self.assertTrue(out.current_position_side_private_rows_present)
        self.assertEqual(out.private_current_row_gap, "CURRENT_SIDE_MODE_ROW_OBSERVED_NONAUTHORIZING")
        self.assertEqual(out.positions_checked, 2)
        self.assertEqual(out.custom_volume_contracts_observed, 2)
        self.assertFalse(out.risk_tiers_current_certified)
        self.assertFalse(out.risk_tiers_at_stress_certified)
        self.assertFalse(out.liquidation_certified)
        self.assertFalse(out.entry_allowed)
        self.assertFalse(out.live_authorization)
        self.assertFalse(out.mexc_orders_sent)

    def test_incremental_by_volume_all_fields_present_but_unmodeled(self):
        f = mixed_with_private_sides()
        for sym in ("TEST_USDT", "SECOND_USDT"):
            increment(f, sym)
        out = self.classify(f)
        self.assertEqual(
            out.status, "MIXED_INCREMENTAL_VOLUME_TERMS_OBSERVED_UNMODELED",
        )
        self.assertTrue(out.incremental_fields_well_formed)
        self.assertEqual(out.incremental_volume_contracts_observed, 2)
        self.assertFalse(out.risk_tiers_at_stress_certified)
        self.assertFalse(out.cross_wallet_collateral_certified)

    def test_incremental_missing_or_malformed_fields_blocks(self):
        f = mixed_with_private_sides()
        increment(f, "TEST_USDT")
        f.public_contract_by_symbol["TEST_USDT"].pop("riskIncrMmr")
        self.assertEqual(
            self.classify(f).status, "MIXED_INCREMENTAL_VOLUME_TERMS_INCOMPLETE",
        )
        f.public_contract_by_symbol["TEST_USDT"]["riskIncrMmr"] = "-1"
        self.assertEqual(
            self.classify(f).status, "MIXED_INCREMENTAL_VOLUME_TERMS_INCOMPLETE",
        )

    def test_private_position_side_and_margin_must_match(self):
        f = mixed()
        out = self.classify(f)
        self.assertEqual(
            out.status,
            "MIXED_TIER_CURRENT_PRIVATE_SIDE_OR_MODE_UNVERIFIED",
        )
        self.assertEqual(out.private_current_row_gap, "PRIVATE_POSITION_SIDE_ROW_ABSENT")
        f = mixed_with_private_sides()
        f.private_risk_by_symbol["SECOND_USDT"][0]["mmr"] = None
        out = self.classify(f)
        self.assertEqual(out.status, "MIXED_TIER_PRIVATE_CURRENT_RATE_FIELDS_UNVERIFIED")
        self.assertEqual(out.private_current_row_gap, "PRIVATE_CURRENT_RATE_FIELDS_UNVERIFIED")

    def test_private_margin_mode_mismatch_and_missing_mode_do_not_authorize(self):
        f = mixed_with_private_sides()
        f.private_risk_by_symbol["SECOND_USDT"][0]["openType"] = 2
        out = self.classify(f)
        self.assertEqual(out.status, "MIXED_TIER_CURRENT_PRIVATE_SIDE_OR_MODE_UNVERIFIED")
        self.assertEqual(out.private_current_row_gap, "PRIVATE_MARGIN_MODE_MISMATCH")
        self.assertFalse(out.live_authorization)
        f.private_risk_by_symbol["SECOND_USDT"][0].pop("openType")
        out = self.classify(f)
        self.assertEqual(out.private_current_row_gap, "PRIVATE_MARGIN_MODE_FIELD_UNVERIFIED")
        self.assertFalse(out.entry_allowed)

    def test_ambiguous_private_same_side_fails_without_order_permit(self):
        f = mixed_with_private_sides()
        f.private_risk_by_symbol["SECOND_USDT"].append(
            copy.deepcopy(f.private_risk_by_symbol["SECOND_USDT"][0])
        )
        out = self.classify(f)
        self.assertEqual(out.private_current_row_gap, "PRIVATE_POSITION_SIDE_ROWS_AMBIGUOUS")
        self.assertFalse(out.risk_tiers_current_certified)
        self.assertFalse(out.live_authorization)
        self.assertNotIn("SECOND_USDT", repr(out))

    def test_public_by_value_or_paused_contract_blocks(self):
        f = mixed_with_private_sides()
        f.public_contract_by_symbol["SECOND_USDT"]["riskLimitType"] = "BY_VALUE"
        self.assertEqual(
            self.classify(f).status,
            "MIXED_TIER_UNSUPPORTED_RISK_SCHEDULE",
        )
        f = mixed_with_private_sides()
        f.public_contract_by_symbol["SECOND_USDT"]["state"] = 4
        self.assertEqual(
            self.classify(f).status,
            "MIXED_TIER_VENUE_CONTRACT_TERMS_UNVERIFIED",
        )

    async def test_two_frame_mixed_mode_witness_reports_gap_only(self):
        f = mixed_with_private_sides()
        for symbol in ("TEST_USDT", "SECOND_USDT"):
            increment(f, symbol)
        second = replace(copy.deepcopy(f), observed_at_ms=1_000_450)
        for fair in second.fair_by_symbol.values():
            fair["observed_at_ms"] = 1_000_450
        obs = await observe_mixed_margin_account_window(
            Reader(f, second), now_ms=lambda: 1_001_000,
        )
        self.assertEqual(
            obs.status,
            "MIXED_ACCOUNT_ECONOMIC_BOOKENDS_OBSERVED_NONAUTHORIZING",
        )
        self.assertEqual(
            obs.mixed_tier_shape_status,
            "MIXED_INCREMENTAL_VOLUME_TERMS_OBSERVED_UNMODELED",
        )
        self.assertTrue(obs.mixed_private_current_side_row_observed)
        self.assertEqual(obs.mixed_private_row_gap, "CURRENT_SIDE_MODE_ROW_OBSERVED_NONAUTHORIZING")
        self.assertTrue(obs.mixed_incremental_terms_present_unmodeled)
        self.assertFalse(obs.future_stressed_tiers_certified)
        self.assertFalse(obs.cross_account_collateral_certified)
        self.assertFalse(obs.live_authorization)
        for raw in ("TEST_USDT", "SECOND_USDT", "500", "250"):
            self.assertNotIn(raw, str(obs))

    def test_no_raw_account_or_prices_in_output(self):
        f = mixed_with_private_sides()
        result = self.classify(f)
        text = repr(result)
        for raw in ("TEST_USDT", "SECOND_USDT", "0.005", "0.02", "100"):
            self.assertNotIn(raw, text)


if __name__ == "__main__":
    unittest.main()
