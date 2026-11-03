"""Offline MEXC market-stop row contract regressions; no exchange POST."""
from __future__ import annotations

from decimal import Decimal as D
import unittest

from app.t100_live_native_stop_execution_audit import (
    assess_native_short_market_stop_row,
)


def row(**overrides):
    r = {
        "id": "99",
        "positionId": "900",
        "symbol": "FOURTH_USDT",
        "state": 1,
        "isFinished": 0,
        "positionType": 2,
        "vol": "250",
        "profitLossVolType": "SAME",
        "volType": 1,
        "stopLossType": 0,
        "stopLossReverse": 2,
        "lossTrend": 1,
        "stopLossPrice": "3.5",
    }
    r.update(overrides)
    return r


def audit(stop):
    return assess_native_short_market_stop_row(
        stop=stop, symbol="FOURTH_USDT", position_id="900",
        position_quantity=D("250"),
        maximum_short_stop_price=D("3.5"),
    )


class NativeMarketStopContractTests(unittest.TestCase):
    def test_observed_market_short_stop_is_not_execution_certificate(self):
        r = audit(row())
        self.assertEqual(
            r.status, "NATIVE_MARKET_SHORT_SL_ROW_OBSERVED_NONAUTHORIZING",
        )
        self.assertTrue(r.active_native_full_volume_market_sl_observed)
        self.assertTrue(r.owned_position_match_verified)
        self.assertFalse(r.stop_execution_certified)
        self.assertFalse(r.exchange_atomicity_certified)
        self.assertFalse(r.account_wide_completeness_certified)
        self.assertFalse(r.cross_margin_liquidation_certified)
        self.assertFalse(r.manual_position_adopted)
        self.assertFalse(r.entry_allowed)
        self.assertFalse(r.live_authorization)
        self.assertFalse(r.mexc_orders_sent)
        self.assertNotIn("FOURTH_USDT", str(r))
        self.assertNotIn("900", str(r))

    def test_documented_entire_position_same_volume_mode(self):
        r = audit(row(volType=2, vol="0", profitLossVolType="SAME"))
        self.assertTrue(r.active_native_full_volume_market_sl_observed)

    def test_documented_separate_sl_volume_mode(self):
        r = audit(row(
            volType=1, profitLossVolType="SEPARATE",
            stopLossVol="250", vol="250",
        ))
        self.assertTrue(r.active_native_full_volume_market_sl_observed)

    def test_limit_stop_reverse_or_wrong_trigger_never_qualifies(self):
        for k,v in (
            ("stopLossType", 1), ("stopLossType", None),
            ("stopLossType", True), ("stopLossType", "0"),
            ("stopLossReverse", 1), ("stopLossReverse", None),
            ("lossTrend", 2), ("lossTrend", 3), ("lossTrend", None),
        ):
            with self.subTest(field=k, value=v):
                r = audit(row(**{k:v}))
                self.assertEqual(
                    r.status,
                    "NATIVE_SL_MARKET_TRIGGER_OR_REVERSE_UNVERIFIED",
                )
                self.assertFalse(
                    r.active_native_full_volume_market_sl_observed,
                )

    def test_stop_is_active_current_short_for_correct_position(self):
        for k,v in (
            ("state", 2), ("state", 3), ("state", True),
            ("isFinished", 1), ("isFinished", None),
            ("positionType", 1), ("positionType", None),
        ):
            with self.subTest(field=k, value=v):
                r = audit(row(**{k:v}))
                self.assertEqual(r.status, "NATIVE_SL_INACTIVE_OR_WRONG_SIDE")

    def test_identity_cannot_be_missing_manual_or_wrong(self):
        for k,v in (
            ("id", 0), ("id", None), ("id", True),
            ("positionId", "901"), ("positionId", None),
            ("positionId", "0"), ("symbol", "OTHER_USDT"),
        ):
            with self.subTest(field=k, value=v):
                r = audit(row(**{k:v}))
                self.assertEqual(
                    r.status, "NATIVE_SL_IDENTITY_OR_OWNERSHIP_UNVERIFIED",
                )

    def test_undercovered_stop_or_looser_short_stop_is_not_full_protection(self):
        for k,v in (
            ("vol", "249"), ("vol", "NaN"), ("vol", "0"),
            ("stopLossVol", "249"),
        ):
            with self.subTest(field=k, value=v):
                bad = row(
                    profitLossVolType="SEPARATE",
                    vol="250", stopLossVol="250",
                )
                bad[k]=v
                if k=="vol":
                    bad["profitLossVolType"]="SAME"
                r = audit(bad)
                if k == "vol" or k == "stopLossVol":
                    self.assertFalse(
                        r.active_native_full_volume_market_sl_observed
                    )
        for v in ("3.50001", "0", "NaN", "Infinity", None):
            with self.subTest(stop=v):
                r = audit(row(stopLossPrice=v))
                self.assertEqual(r.status, "NATIVE_SL_PRICE_NOT_PROTECTIVE")

    def test_unknown_volume_modes_never_certify_full_native_coverage(self):
        for change in (
            {"volType": None}, {"volType": 3},
            {"profitLossVolType": None},
            {"profitLossVolType": "UNKNOWN"},
            {"profitLossVolType": ["SAME"]},
        ):
            with self.subTest(change=change):
                r = audit(row(vol="250", **change))
                self.assertEqual(r.status, "NATIVE_SL_FULL_CONTRACT_COVERAGE_UNVERIFIED")
                self.assertFalse(r.active_native_full_volume_market_sl_observed)
                self.assertFalse(r.live_authorization)

    def test_malformed_unicode_id_fails_closed_without_exception(self):
        for value in ("²", "９９", "99x", "0", "", True):
            with self.subTest(value=value):
                r = audit(row(id=value))
                self.assertEqual(r.status, "NATIVE_SL_IDENTITY_OR_OWNERSHIP_UNVERIFIED")

    def test_bad_expected_position_never_qualifies(self):
        for bad in (D("0"), D("-1"), D("NaN"), None, 250):
            with self.subTest(qty=bad):
                r = assess_native_short_market_stop_row(
                    stop=row(), symbol="FOURTH_USDT", position_id="900",
                    position_quantity=bad,
                    maximum_short_stop_price=D("3.5"),
                )
                self.assertEqual(r.status,"NATIVE_SL_AUDIT_INPUT_INVALID")


if __name__ == "__main__":
    unittest.main()
