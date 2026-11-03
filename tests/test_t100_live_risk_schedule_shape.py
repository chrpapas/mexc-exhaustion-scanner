"""Public contract risk-mode shape tests; never compute liquidations."""
from __future__ import annotations

import copy
import unittest

from app.t100_live_risk_schedule_shape import classify_risk_schedule_shapes
from tests.test_t100_live_risk_tier_witness import public


class RiskScheduleShapeTests(unittest.TestCase):
    def test_supported_static_custom_volume_classified_but_not_authorized(self):
        shape = classify_risk_schedule_shapes({"TEST_USDT": public()})
        self.assertEqual(shape.mode, "CUSTOM")
        self.assertEqual(shape.limit_type, "BY_VOLUME")
        self.assertEqual(shape.schedule_fields, "CUSTOM_FIELDS_PRESENT")
        self.assertFalse(shape.modeled_future_tiers)
        self.assertFalse(shape.entry_allowed)
        self.assertFalse(shape.live_authorization)

    def test_custom_value_is_not_claimed_as_static_volume(self):
        contract = public()
        contract["riskLimitType"] = "BY_VALUE"
        shape = classify_risk_schedule_shapes({"TEST_USDT": contract})
        self.assertEqual(shape.mode, "CUSTOM")
        self.assertEqual(shape.limit_type, "BY_VALUE")
        self.assertFalse(shape.modeled_future_tiers)

    def test_incremental_by_volume_fields_present_but_unmodeled(self):
        contract = public()
        contract.update({
            "riskLimitMode": "INCREASE",
            "riskLimitType": "BY_VOLUME",
            "riskBaseVol": 9000,
            "riskIncrVol": 9000,
            "riskIncrMmr": 0.005,
            "riskIncrImr": 0.005,
            "riskLevelLimit": 1,
            "maintenanceMarginRate": 0.01,
            "initialMarginRate": 0.02,
        })
        shape = classify_risk_schedule_shapes({"TEST_USDT": contract})
        self.assertEqual(shape.mode, "INCREASE")
        self.assertEqual(shape.limit_type, "BY_VOLUME")
        self.assertEqual(
            shape.schedule_fields, "INCREMENTAL_FIELDS_PRESENT_UNMODELED",
        )
        self.assertFalse(shape.modeled_future_tiers)

    def test_incremental_fields_missing_or_incoherent_are_distinct(self):
        contract = public()
        contract["riskLimitMode"] = "INCREASE"
        shape = classify_risk_schedule_shapes({"TEST_USDT": contract})
        self.assertEqual(
            shape.schedule_fields, "INCREMENTAL_FIELDS_MISSING_OR_MALFORMED",
        )
        contract.update({
            "riskBaseVol": 9000, "riskIncrVol": 0, "riskIncrMmr": 0.005,
            "riskIncrImr": 0.005, "riskLevelLimit": 3,
            "maintenanceMarginRate": 0.01, "initialMarginRate": 0.02,
        })
        shape = classify_risk_schedule_shapes({"TEST_USDT": contract})
        self.assertEqual(shape.schedule_fields, "INCREMENTAL_FIELDS_INCOHERENT")

    def test_mixed_types_and_unknown_do_not_assume_best_case(self):
        first = public()
        second = copy.deepcopy(first)
        second["riskLimitType"] = "BY_VALUE"
        shape = classify_risk_schedule_shapes({
            "AAA_USDT": first, "BBB_USDT": second,
        })
        self.assertEqual(shape.limit_type, "MIXED")
        self.assertEqual(shape.mode, "CUSTOM")
        self.assertFalse(shape.live_authorization)
        other = copy.deepcopy(first)
        other["riskLimitMode"] = "SECRET_UNKNOWN_MODE"
        shape = classify_risk_schedule_shapes({"AAA_USDT": other})
        self.assertEqual(shape.mode, "UNKNOWN")
        self.assertEqual(shape.schedule_fields, "UNVERIFIED")
        self.assertNotIn("SECRET", str(shape))

    def test_bad_input_does_not_leak_contract_values(self):
        for x in (None, {}, {"SECRET_USDT": "SECRET_OBJECT"}, {
            str(i): public() for i in range(5)
        }):
            with self.subTest(x=str(type(x))):
                shape = classify_risk_schedule_shapes(x)
                self.assertEqual(shape.mode, "UNKNOWN")
                self.assertEqual(shape.schedule_fields, "UNVERIFIED")
                self.assertNotIn("SECRET", str(shape))
                self.assertFalse(shape.entry_allowed)


if __name__ == "__main__":
    unittest.main()
