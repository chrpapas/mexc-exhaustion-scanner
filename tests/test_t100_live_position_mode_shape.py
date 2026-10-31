"""Offline current-account MEXC position-shape classification tests.

Manual longs and isolated positions are observable, not T100 violations.
The original ALL CROSS SHORT risk witness remains strictly nonauthorizing.
"""
from __future__ import annotations

import unittest
from copy import deepcopy

from app.t100_live_position_mode_shape import classify_account_position_modes
from app.t100_live_cross_risk_diagnostic import run_diagnostic
from app.t100_live_readonly_probe import ReadOnlyMexcProbe
from tests.test_t100_live_cross_risk_collector import FakePrivate, FakePublic
from tests.test_t100_live_cross_risk_observation import frame


class AccountModeShapeTests(unittest.IsolatedAsyncioTestCase):
    def test_two_manual_positions_mixed_long_and_isolated_are_classified(self):
        p = frame().positions[0]
        a = deepcopy(p)
        a["positionId"] = "123"
        a["symbol"] = "FIRST_USDT"
        a["positionType"] = 1
        a["openType"] = 2
        b = deepcopy(p)
        b["positionId"] = "456"
        b["symbol"] = "SECOND_USDT"
        b["positionType"] = 2
        b["openType"] = 1
        r = classify_account_position_modes([a, b])
        self.assertEqual(
            r.status, "ACCOUNT_MIXED_SIDE_OR_MARGIN_OBSERVED_NONAUTHORIZING",
        )
        self.assertEqual(r.total_positions_observed, 2)
        self.assertEqual(r.cross_long_count, 1)
        self.assertEqual(r.isolated_short_count, 1)
        self.assertEqual(r.cross_short_count, 0)
        self.assertEqual(r.isolated_long_count, 0)
        self.assertFalse(r.t100_position_ownership_verified)
        self.assertFalse(r.cross_collateral_certified)
        self.assertFalse(r.liquidation_risk_certified)
        self.assertFalse(r.entry_allowed)
        self.assertFalse(r.live_authorization)
        for private in ("FIRST_USDT", "SECOND_USDT", "123", "456"):
            self.assertNotIn(private, str(r))

    def test_unknown_mode_side_bad_id_and_duplicate_fail_closed(self):
        p = frame().positions[0]
        for key, val in (
            ("openType", None),
            ("openType", 3),
            ("openType", "2"),
            ("positionType", 0),
            ("positionType", "1"),
            ("positionId", "ABC123"),
            ("holdVol", "NaN"),
        ):
            bad = dict(p)
            bad[key] = val
            with self.subTest(key=key, val=val):
                r = classify_account_position_modes([bad])
                self.assertEqual(
                    r.status, "POSITION_MODE_OR_IDENTITY_UNVERIFIED"
                )
                self.assertIsNone(r.total_positions_observed)
                self.assertFalse(r.entry_allowed)
        duplicate = classify_account_position_modes([p, deepcopy(p)])
        self.assertEqual(
            duplicate.status, "POSITION_MODE_OR_IDENTITY_UNVERIFIED"
        )
        self.assertIsNone(duplicate.cross_short_count)

    async def test_isolated_manual_position_failure_exposes_mode_not_account_data(self):
        x = frame()
        x.positions[0]["openType"] = 1
        private = FakePrivate(x)
        result = await run_diagnostic(
            private=ReadOnlyMexcProbe(private),
            public=FakePublic(x, fair_ms=1_000_400),
            clock_ms=iter([1_000_000, 1_000_500, 1_000_700]).__next__,
        )
        self.assertEqual(
            result["failure_code"], "POSITION_NOT_VERIFIED_CROSS_SHORT",
        )
        self.assertEqual(result["failure_stage"], "VALIDATE_FIRST")
        self.assertEqual(
            result["account_position_mode_shape_status"],
            "ACCOUNT_MIXED_SIDE_OR_MARGIN_OBSERVED_NONAUTHORIZING",
        )
        self.assertEqual(result["account_isolated_short_positions"], 1)
        self.assertEqual(result["account_cross_short_positions"], 0)
        self.assertEqual(result["account_cross_long_positions"], 0)
        self.assertEqual(result["account_position_mode_rows_observed"], 1)
        self.assertFalse(result["account_position_mode_observation_authorizing"])
        self.assertFalse(result["venue_liquidation_certified"])
        self.assertFalse(result["entry_allowed"])
        self.assertFalse(result["live_authorization"])
        self.assertFalse(result["mexc_orders_sent"])
        self.assertTrue(all(method == "GET" for method, *_ in private.calls))
        self.assertNotIn("TEST_USDT", str(result))

    async def test_cross_manual_long_is_not_dropped_from_risk_inventory(self):
        x = frame()
        x.positions[0]["positionType"] = 1
        private = FakePrivate(x)
        result = await run_diagnostic(
            private=ReadOnlyMexcProbe(private),
            public=FakePublic(x, fair_ms=1_000_400),
            clock_ms=iter([1_000_000, 1_000_500, 1_000_700]).__next__,
        )
        self.assertEqual(
            result["failure_code"], "POSITION_NOT_VERIFIED_CROSS_SHORT"
        )
        self.assertEqual(result["account_cross_long_positions"], 1)
        self.assertEqual(result["account_cross_short_positions"], 0)
        self.assertIsNone(result["observed_positions"])
        self.assertFalse(result["account_wide_completeness_certified"])
        self.assertFalse(result["cross_wallet_collateral_certified"])
        self.assertFalse(result["live_authorization"])

    def test_all_cross_short_shape_is_still_observation_only(self):
        out = classify_account_position_modes(frame().positions)
        self.assertEqual(
            out.status, "ACCOUNT_ALL_CROSS_SHORT_OBSERVED_NONAUTHORIZING",
        )
        self.assertEqual(out.cross_short_count, 1)
        self.assertFalse(out.cross_collateral_certified)
        self.assertFalse(out.entry_allowed)


if __name__ == "__main__":
    unittest.main()
