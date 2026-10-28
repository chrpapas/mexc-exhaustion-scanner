"""Pure schema and tier-drift tests. Numbers are fictional fixture values."""
from __future__ import annotations

import copy
import unittest
from decimal import Decimal as D

from app.t100_live_risk_tier_witness import assess_risk_tier_witness


def private():
    return {"TEST_USDT": [{
        "symbol": "TEST_USDT", "positionType": 2, "openType": 2,
        "level": 1, "maxVol": 100, "maxLeverage": 10,
        "leverage": 5, "mmr": 0.01, "imr": 0.02,
        "currentMmr": 0.01,
    }]}


def public():
    return {
        "symbol": "TEST_USDT", "positionOpenType": 3,
        "riskLimitMode": "CUSTOM", "riskLimitType": "BY_VOLUME",
        "state": 0, "apiAllowed": True,
        "futureType": 1, "quoteCoin": "USDT", "settleCoin": "USDT",
        "contractSize": 2, "liquidationFeeRate": 0.005,
        "minLeverage": 1, "maxLeverage": 20,
        "countryConfigContractMaxLeverage": 0,
        "riskLimitCustom": [
            {"level": 1, "maxVol": 100, "mmr": 0.01, "imr": 0.02,
             "maxLeverage": 10},
            {"level": 2, "maxVol": 300, "mmr": 0.04, "imr": 0.05,
             "maxLeverage": 5},
            {"level": 3, "maxVol": 1000, "mmr": 0.08, "imr": 0.09,
             "maxLeverage": 3},
        ],
    }


def run(n=50, pr=None, pub=None):
    return assess_risk_tier_witness(
        symbol="TEST_USDT", contracts=n,
        private_risk=private() if pr is None else pr,
        public_contract=public() if pub is None else pub,
    )


class RiskTierWitnessTests(unittest.TestCase):
    def test_current_custom_volume_schedule_observed_without_future_certification(self):
        result = run()
        self.assertEqual(
            result.status,
            "CURRENT_CUSTOM_VOLUME_RISK_SCHEDULE_OBSERVED_NONAUTHORIZING",
        )
        self.assertEqual(result.current_private_level, 1)
        self.assertEqual(result.modeled_volume_tier, 1)
        self.assertEqual(result.conservative_schedule_mmr, D("0.08"))
        self.assertEqual(result.liquidation_fee_rate, D("0.005"))
        self.assertEqual(result.contract_size, D("2"))
        self.assertEqual(result.configured_leverage, 5)
        self.assertFalse(result.stressed_tier_upper_bound_verified)
        self.assertFalse(result.entry_allowed)
        self.assertFalse(result.live_authorization)

    def test_private_short_side_plus_long_side_preserves_short_only(self):
        pr = private()
        pr["TEST_USDT"].append({
            "symbol": "TEST_USDT", "positionType": 1,
            "level": 1,
        })
        self.assertTrue(run(pr=pr).current_risk_row_observed)

    def test_value_based_tier_schedule_never_inferred_from_current_maxvol(self):
        pub = public()
        pub["riskLimitType"] = "BY_VALUE"
        self.assertEqual(
            run(pub=pub).status,
            "RISK_TIER_STATIC_VOLUME_SCHEDULE_UNAVAILABLE",
        )

    def test_incremental_schedule_not_reconstructed_from_partial_metadata(self):
        pub = public()
        pub["riskLimitMode"] = "INCREASE"
        self.assertEqual(
            run(pub=pub).status,
            "RISK_TIER_STATIC_VOLUME_SCHEDULE_UNAVAILABLE",
        )

    def test_isolated_position_mode_blocks(self):
        pr = private()
        pr["TEST_USDT"][0]["openType"] = 1
        self.assertEqual(
            run(pr=pr).status,
            "RISK_TIER_PRIVATE_CROSS_OR_CAP_UNVERIFIED",
        )

    def test_private_max_vol_too_small_blocks(self):
        self.assertEqual(
            run(n=101).status,
            "RISK_TIER_PRIVATE_CROSS_OR_CAP_UNVERIFIED",
        )

    def test_unavailable_or_paused_contract_blocks(self):
        for field, value in (
            ("state", 4), ("apiAllowed", False),
            ("positionOpenType", 1), ("settleCoin", "BTC"),
        ):
            with self.subTest(field=field):
                pub = public()
                pub[field] = value
                self.assertEqual(
                    run(pub=pub).status, "RISK_TIER_PUBLIC_CONTRACT_NOT_ELIGIBLE",
                )

    def test_country_leverage_limit_honored(self):
        pub = public()
        pub["countryConfigContractMaxLeverage"] = 3
        self.assertEqual(
            run(pub=pub).status, "RISK_TIER_PUBLIC_LEVERAGE_LIMIT",
        )

    def test_private_mmr_cannot_be_below_public_current_level(self):
        pr = private()
        pr["TEST_USDT"][0]["mmr"] = 0.001
        self.assertEqual(
            run(pr=pr).status, "RISK_TIER_PRIVATE_PUBLIC_MMR_CONFLICT",
        )

    def test_private_current_mmr_conflict_blocks(self):
        pr = private()
        pr["TEST_USDT"][0]["currentMmr"] = 0.001
        self.assertEqual(
            run(pr=pr).status, "RISK_TIER_PRIVATE_PUBLIC_MMR_CONFLICT",
        )

    def test_missing_custom_levels_and_noncontiguous_levels_block(self):
        pub = public()
        pub["riskLimitCustom"] = []
        self.assertEqual(
            run(pub=pub).status, "RISK_TIER_CUSTOM_LEVELS_MISSING",
        )
        pub = public()
        pub["riskLimitCustom"][1]["level"] = 4
        self.assertEqual(
            run(pub=pub).status, "RISK_TIER_LEVEL_SEQUENCE_INVALID",
        )

    def test_decreasing_volume_caps_or_inverted_mmr_blocks(self):
        for field, value in (
            ("maxVol", 100), ("mmr", 0.001),
            ("imr", 0.0001), ("maxLeverage", 50),
        ):
            pub = public()
            pub["riskLimitCustom"][1][field] = value
            with self.subTest(field=field):
                self.assertEqual(
                    run(pub=pub).status, "RISK_TIER_LEVEL_MONOTONICITY_INVALID",
                )

    def test_private_level_outside_complete_schedule_blocks(self):
        pr = private()
        pr["TEST_USDT"][0]["level"] = 10
        self.assertEqual(
            run(pr=pr).status, "RISK_TIER_PRIVATE_LEVEL_NOT_IN_SCHEDULE",
        )

    def test_wrong_symbol_and_bad_contract_size_block(self):
        pr = private()
        pr["TEST_USDT"][0]["symbol"] = "OTHER_USDT"
        self.assertEqual(
            run(pr=pr).status, "RISK_TIER_PRIVATE_SYMBOL_DRIFT",
        )
        pub = public()
        pub["contractSize"] = "NaN"
        self.assertEqual(
            run(pub=pub).status, "RISK_TIER_INPUT_INVALID",
        )

    def test_duplicate_short_rows_block(self):
        pr = private()
        pr["TEST_USDT"].append(copy.deepcopy(pr["TEST_USDT"][0]))
        self.assertEqual(
            run(pr=pr).status, "RISK_TIER_PRIVATE_SHORT_NOT_UNIQUE",
        )

    def test_nonfinite_and_boolean_contracts_block(self):
        for bad in (True, False, float("nan"), "Infinity", 0, -5):
            with self.subTest(bad=bad):
                self.assertEqual(
                    run(n=bad).status, "RISK_TIER_INPUT_INVALID",
                )

    def test_no_implicit_leverage_expansion(self):
        pub = public()
        pub["riskLimitCustom"][0]["maxLeverage"] = 4
        pub["riskLimitCustom"][1]["maxLeverage"] = 4
        self.assertEqual(
            run(pub=pub).status,
            "RISK_TIER_POSITION_LEVERAGE_EXCEEDS_TIER",
        )


if __name__ == "__main__":
    unittest.main()
