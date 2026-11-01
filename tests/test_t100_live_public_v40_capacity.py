"""Offline tests of PUBLIC T100/V40 notional -> contract capacity."""
from __future__ import annotations

import unittest
from decimal import Decimal as D

from app.t100_live_public_v40_capacity import assess_public_v40_capacity
from tests.test_t100_live_incremental_tier_candidate import incr
from tests.test_t100_live_risk_tier_witness import public


def siren():
    row = incr()
    row.update({
        "symbol": "SIREN_USDT",
        "contractSize": 10,
        "volUnit": 1,
        "minVol": 1,
        "maxVol": 3600,
        "riskLimitMode": "INCREASE",
        "riskLimitType": "BY_VOLUME",
        "riskLevelLimit": 1,
        "riskLongShortSwitch": 0,
        "riskBaseVol": 3600,
        "riskIncrVol": 0,
    })
    return row


class PublicV40CapacityTests(unittest.TestCase):
    def test_siren_example_5625_usdt_exceeds_published_3600_lot_cap(self):
        r = assess_public_v40_capacity(
            siren(), desired_notional_usdt="5625",
            entry_reference_price="0.02298",
        )
        self.assertEqual(
            r.status,
            "V40_CAPACITY_PUBLISHED_POSITION_TIER_EXCEEDED_NONAUTHORIZING",
        )
        self.assertEqual(r.required_lots_rounded_down, D("24477"))
        self.assertEqual(r.public_max_single_order_lots, D("3600"))
        self.assertEqual(r.public_max_position_tier_lots, D("3600"))
        self.assertEqual(r.public_max_position_notional_usdt, D("827.28000"))
        self.assertFalse(r.fits_public_single_order_limit)
        self.assertFalse(r.fits_public_position_tier)
        self.assertFalse(r.private_current_cap_verified)
        self.assertFalse(r.entry_allowed)
        self.assertFalse(r.live_authorization)

    def test_small_siren_hypothetical_fits_but_is_not_authorized(self):
        r = assess_public_v40_capacity(
            siren(), desired_notional_usdt="500",
            entry_reference_price="0.02298",
        )
        self.assertEqual(
            r.status, "V40_CAPACITY_PUBLISHED_SINGLE_ORDER_FITS_NONAUTHORIZING",
        )
        self.assertEqual(r.required_lots_rounded_down, D("2175"))
        self.assertTrue(r.fits_public_single_order_limit)
        self.assertTrue(r.fits_public_position_tier)
        self.assertLessEqual(r.modeled_lots_notional_usdt,
                             r.desired_notional_usdt)
        self.assertEqual(
            r.modeled_stop_gap_notional_usdt,
            r.modeled_lots_notional_usdt * D("1.80"),
        )
        for flag in (
            "private_current_cap_verified", "private_current_cross_verified",
            "account_specific_order_access_verified",
            "depth_and_execution_capacity_verified",
            "future_tiers_certified", "account_collateral_certified",
            "native_stop_protection_verified", "entry_allowed",
            "live_authorization", "mexc_orders_sent",
        ):
            self.assertFalse(getattr(r, flag))

    def test_full_225pct_per_slot_2k_equity_also_exceeds_siren_capacity(self):
        r = assess_public_v40_capacity(
            siren(), desired_notional_usdt=D("2000")*D("2.25")/4,
            entry_reference_price="0.02298",
        )
        self.assertEqual(
            r.status,
            "V40_CAPACITY_PUBLISHED_POSITION_TIER_EXCEEDED_NONAUTHORIZING",
        )

    def test_different_single_order_cap_and_total_tier(self):
        row = siren()
        row["maxVol"] = 500
        result = assess_public_v40_capacity(
            row, desired_notional_usdt="200",
            entry_reference_price="0.02298",
        )
        self.assertEqual(
            result.status,
            "V40_CAPACITY_PUBLISHED_SINGLE_ORDER_LIMIT_EXCEEDED_NONAUTHORIZING",
        )
        self.assertTrue(result.fits_public_position_tier)
        self.assertFalse(result.fits_public_single_order_limit)

    def test_custom_by_volume_schedule_and_step_round_down(self):
        row = public()
        row.update({"minVol": 1, "maxVol": 1000,
                    "volUnit": 1})
        result = assess_public_v40_capacity(
            row, desired_notional_usdt="100.90",
            entry_reference_price="1",
        )
        self.assertEqual(
            result.status, "V40_CAPACITY_PUBLISHED_SINGLE_ORDER_FITS_NONAUTHORIZING",
        )
        self.assertEqual(result.required_lots_rounded_down, D("50"))
        self.assertEqual(result.modeled_lots_notional_usdt, D("100"))
        self.assertEqual(result.public_max_position_tier_lots, D("1000"))

    def test_below_minimum_order_no_upward_rounding(self):
        r = assess_public_v40_capacity(
            siren(), desired_notional_usdt="0.01",
            entry_reference_price="0.02298",
        )
        self.assertEqual(
            r.status, "V40_CAPACITY_TARGET_BELOW_MINIMUM_NONAUTHORIZING",
        )
        self.assertIsNone(r.required_lots_rounded_down)
        self.assertFalse(r.entry_allowed)

    def test_reject_unknown_or_value_tier_and_api_denial(self):
        for key, val in (
            ("riskLimitType", "BY_VALUE"),
            ("riskLimitMode", "UNKNOWN"),
            ("apiAllowed", False),
            ("positionOpenType", 1),
        ):
            row = siren()
            row[key] = val
            result = assess_public_v40_capacity(
                row, desired_notional_usdt="500",
                entry_reference_price="0.02298",
            )
            self.assertEqual(
                result.status, "V40_CAPACITY_PUBLIC_SCHEDULE_OR_ELIGIBILITY_BLOCKED",
            )

    def test_reject_bad_qty_increment_price_or_venue_schema(self):
        for key, val in (
            ("volUnit", 0), ("volUnit", "NaN"),
            ("minVol", 0), ("maxVol", 0),
            ("maxVol", 0.5), ("contractSize", -1),
        ):
            row = siren()
            row[key] = val
            result = assess_public_v40_capacity(
                row, desired_notional_usdt="500",
                entry_reference_price="0.02298",
            )
            self.assertNotEqual(
                result.status,
                "V40_CAPACITY_PUBLISHED_SINGLE_ORDER_FITS_NONAUTHORIZING",
            )
        for value in (None, True, "NaN", "-3", "Infinity"):
            result = assess_public_v40_capacity(
                siren(), desired_notional_usdt=value,
                entry_reference_price="0.02298",
            )
            self.assertEqual(result.status, "V40_CAPACITY_INVALID_OR_INCOMPLETE")

    def test_offline_module_never_reaches_order_adapter(self):
        import inspect
        import app.t100_live_public_v40_capacity as module
        text = inspect.getsource(module)
        for forbidden in (
            "._private_post(", ".send_one(", "MexcTradeClient(",
            "LiveMutationPermit", "httpx.", "asyncpg.",
        ):
            self.assertNotIn(forbidden, text)


if __name__ == "__main__":
    unittest.main()
