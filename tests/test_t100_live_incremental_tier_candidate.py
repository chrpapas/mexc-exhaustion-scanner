"""Offline INCREASE/BY_VOLUME MEXC candidate reconstruction tests."""
import copy
import unittest
from decimal import Decimal as D

from app.t100_live_incremental_tier_candidate import reconstruct_incremental_candidate
from tests.test_t100_live_risk_tier_witness import public, private


def incr():
    row = public()
    row.update({
        "riskLimitMode": "INCREASE",
        "riskLimitType": "BY_VOLUME",
        "riskLongShortSwitch": 0,
        "riskBaseVol": 100,
        "riskIncrVol": 100,
        "riskLevelLimit": 3,
        "maintenanceMarginRate": 0.01,
        "initialMarginRate": 0.02,
        "riskIncrMmr": 0.01,
        "riskIncrImr": 0.01,
    })
    return row


class IncrementalCandidateTests(unittest.TestCase):
    def test_formula_boundaries_and_zero_authorization(self):
        expected = [(1, "100", "0.01"), (2, "200", "0.02"), (3, "300", "0.03")]
        for lots, level in ((1, 1), (100, 1), (101, 2), (200, 2), (201, 3), (300, 3)):
            with self.subTest(lots=lots):
                r = reconstruct_incremental_candidate(incr(), position_contracts=lots)
                self.assertEqual(r.status, "INCR_CANDIDATE_RECONSTRUCTED_NONAUTHORIZING")
                self.assertEqual(r.selected_position_tier, level)
                self.assertEqual(
                    [(t.level, str(t.max_contracts), str(t.mmr)) for t in r.tiers],
                    expected,
                )
                self.assertFalse(r.future_stressed_tiers_certified)
                self.assertFalse(r.venue_liquidation_certified)
                self.assertFalse(r.entry_allowed)
                self.assertFalse(r.live_authorization)

    def test_current_private_short_mmr_is_compared_not_certified(self):
        short = copy.deepcopy(private()["TEST_USDT"][0])
        short["mmr"] = 0.01
        short["currentMmr"] = 0.01
        r = reconstruct_incremental_candidate(
            incr(), position_contracts=50, private_short_risk=short,
        )
        self.assertEqual(r.private_current_comparison, "PRIVATE_CURRENT_MMR_EXACT_MATCH")
        self.assertTrue(r.private_cross_short_verified)
        self.assertFalse(r.live_authorization)
        short["currentMmr"] = 0.02
        r = reconstruct_incremental_candidate(
            incr(), position_contracts=50, private_short_risk=short,
        )
        self.assertEqual(r.private_current_comparison, "PRIVATE_CURRENT_MMR_ABOVE_CANDIDATE")
        short["currentMmr"] = 0.005
        r = reconstruct_incremental_candidate(
            incr(), position_contracts=50, private_short_risk=short,
        )
        self.assertEqual(r.private_current_comparison, "PRIVATE_CURRENT_MMR_BELOW_CANDIDATE")

    def test_short_side_switch_uses_short_not_general_limits(self):
        row = incr()
        row["riskLongShortSwitch"] = 1
        row["riskBaseVolShort"] = 60
        row["riskIncrVolShort"] = 50
        r = reconstruct_incremental_candidate(row, position_contracts=100)
        self.assertEqual(r.selected_position_tier, 2)
        self.assertTrue(r.short_specific_limits_used)
        row.pop("riskBaseVolShort")
        r = reconstruct_incremental_candidate(row, position_contracts=100)
        self.assertEqual(r.status, "INCR_SHORT_SIDE_FIELDS_MISSING")

    def test_one_tier_zero_increment_valid(self):
        row = incr()
        row["riskLevelLimit"] = 1
        row["riskIncrVol"] = 0
        row["riskIncrMmr"] = 0
        row["riskIncrImr"] = 0
        r = reconstruct_incremental_candidate(row, position_contracts=100)
        self.assertEqual(r.selected_position_tier, 1)
        self.assertEqual(len(r.tiers), 1)

    def test_position_above_published_cap_blocks(self):
        self.assertEqual(
            reconstruct_incremental_candidate(incr(), position_contracts=301).status,
            "INCR_POSITION_OUTSIDE_PUBLISHED_CANDIDATE"
        )

    def test_by_value_and_custom_never_modeled(self):
        for key, value in (("riskLimitType", "BY_VALUE"), ("riskLimitMode", "CUSTOM")):
            row = incr()
            row[key] = value
            self.assertEqual(
                reconstruct_incremental_candidate(row, position_contracts=10).status,
                "INCR_MODE_OR_TYPE_UNSUPPORTED",
            )

    def test_private_isolated_and_out_of_schedule_block(self):
        short = copy.deepcopy(private()["TEST_USDT"][0])
        short["openType"] = 1
        self.assertEqual(
            reconstruct_incremental_candidate(
                incr(), position_contracts=50, private_short_risk=short
            ).status, "INCR_PRIVATE_CROSS_SHORT_UNVERIFIED",
        )
        short["openType"] = 2
        short["level"] = 5
        self.assertEqual(
            reconstruct_incremental_candidate(
                incr(), position_contracts=50, private_short_risk=short
            ).status, "INCR_PRIVATE_LEVEL_OUTSIDE_PUBLIC_SCHEDULE",
        )

    def test_invalid_increments_and_rate_breach_block(self):
        for key, value in (
            ("riskIncrVol", 0), ("riskIncrMmr", -0.01),
            ("maintenanceMarginRate", "NaN"), ("riskLevelLimit", True),
            ("riskIncrImr", 1), ("riskBaseVol", 0),
            ("initialMarginRate", 0.001), ("riskLongShortSwitch", 2),
        ):
            row = incr()
            row[key] = value
            r = reconstruct_incremental_candidate(row, position_contracts=50)
            self.assertNotEqual(r.status, "INCR_CANDIDATE_RECONSTRUCTED_NONAUTHORIZING")
            self.assertFalse(r.live_authorization)

    def test_contract_ineligible_never_modeled(self):
        row = incr()
        row["apiAllowed"] = False
        r = reconstruct_incremental_candidate(row, position_contracts=10)
        self.assertEqual(r.status, "INCR_CONTRACT_NOT_ELIGIBLE")

    def test_private_below_cannot_turn_into_compliant_certificate(self):
        short = copy.deepcopy(private()["TEST_USDT"][0])
        short["mmr"] = 0.001
        short["currentMmr"] = 0.001
        r = reconstruct_incremental_candidate(
            incr(), position_contracts=50, private_short_risk=short,
        )
        self.assertEqual(r.private_current_comparison, "PRIVATE_CURRENT_MMR_BELOW_CANDIDATE")
        self.assertFalse(r.future_stressed_tiers_certified)


if __name__ == "__main__":
    unittest.main()
