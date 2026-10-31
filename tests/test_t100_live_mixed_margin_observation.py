"""Offline mixed cross-short / isolated-long account stress evidence tests.

Economic bookends are not proof of collateral, liquidation, native stops,
future stressed tiers or order authorization. No actual MEXC or network.
"""
from __future__ import annotations

import copy
import unittest
from dataclasses import replace

from app.t100_live_mixed_margin_observation import (
    observe_mixed_margin_account_window,
)
from app.t100_live_cross_risk_diagnostic import run_diagnostic
from app.t100_live_readonly_probe import ReadOnlyMexcProbe
from tests.test_t100_live_cross_risk_observation import frame
from tests.test_t100_live_cross_risk_collector import FakePrivate, FakePublic


def mixed(ts=1_000_000):
    data = frame(ts)
    second = copy.deepcopy(data.positions[0])
    second.update({
        "symbol": "SECOND_USDT",
        "positionId": "14",
        "openType": 1,
        "positionType": 1,
        "im": "30",
        "holdVol": "10",
        "unRealizedPnl": "-3.5",
    })
    data.positions.append(second)
    data.assets[0]["positionMargin"] = "50"
    data.assets[0]["unrealized"] = "-3.5"
    second_risk = copy.deepcopy(data.private_risk_by_symbol["TEST_USDT"])
    for row in second_risk:
        row["symbol"] = "SECOND_USDT"
    data.private_risk_by_symbol["SECOND_USDT"] = second_risk
    contract = copy.deepcopy(data.public_contract_by_symbol["TEST_USDT"])
    contract["symbol"] = "SECOND_USDT"
    data.public_contract_by_symbol["SECOND_USDT"] = contract
    data.fair_by_symbol["SECOND_USDT"] = {
        "fairPrice": "2", "observed_at_ms": ts,
    }
    return data


class Reader:
    def __init__(self, *frames):
        self.frames = list(frames)
        self.calls = 0

    async def read(self):
        self.calls += 1
        return copy.deepcopy(self.frames.pop(0))


class MixedAccountObserverTests(unittest.IsolatedAsyncioTestCase):
    async def test_two_frame_cross_short_plus_isolated_long_negative_pnl(self):
        read = Reader(mixed(), mixed(1_000_450))
        obs = await observe_mixed_margin_account_window(
            read, now_ms=lambda: 1_001_000,
        )
        self.assertEqual(
            obs.status,
            "MIXED_ACCOUNT_ECONOMIC_BOOKENDS_OBSERVED_NONAUTHORIZING",
        )
        self.assertEqual(obs.positions_observed, 2)
        self.assertEqual(obs.cross_margin_positions, 1)
        self.assertEqual(obs.isolated_margin_positions, 1)
        self.assertTrue(obs.economic_parity_observed)
        self.assertTrue(obs.stable_critical_inventory)
        self.assertTrue(obs.fair_price_window_observed)
        self.assertFalse(obs.isolated_unrealized_counted_as_cross_collateral)
        self.assertFalse(obs.cross_account_collateral_certified)
        self.assertFalse(obs.current_risk_tiers_certified)
        self.assertFalse(obs.future_stressed_tiers_certified)
        self.assertFalse(obs.venue_liquidation_certified)
        self.assertFalse(obs.entry_allowed)
        self.assertFalse(obs.live_authorization)
        self.assertFalse(obs.mexc_orders_sent)
        self.assertEqual(read.calls, 2)

    async def test_isolated_margin_implicit_double_use_not_certified(self):
        data = mixed()
        data.assets[0]["positionMargin"] = "20"
        obs = await observe_mixed_margin_account_window(
            Reader(data, mixed(1_000_450)),
            now_ms=lambda: 1_001_000,
        )
        self.assertEqual(obs.status, "MIXED_ACCOUNT_READ_OR_SCHEMA_UNVERIFIED")
        self.assertFalse(obs.cross_account_collateral_certified)

    async def test_mixed_position_identity_or_leverage_change_blocks(self):
        altered = mixed(1_000_450)
        altered.positions[1]["openType"] = 2
        obs = await observe_mixed_margin_account_window(
            Reader(mixed(), altered),
            now_ms=lambda: 1_001_000,
        )
        self.assertEqual(obs.status, "MIXED_ACCOUNT_CRITICAL_INVENTORY_DRIFT")
        self.assertFalse(obs.entry_allowed)

    async def test_mixed_position_sl_not_required_and_unrelated_tp_record_observed(self):
        first = mixed()
        second = mixed(1_000_450)
        for x in (first, second):
            x.stops.clear()
        obs = await observe_mixed_margin_account_window(
            Reader(first, second),
            now_ms=lambda: 1_001_000,
        )
        self.assertEqual(
            obs.status,
            "MIXED_ACCOUNT_ECONOMIC_BOOKENDS_OBSERVED_NONAUTHORIZING",
        )
        self.assertFalse(obs.entry_allowed)

    async def test_excessive_fair_change_blocks_without_venue_values(self):
        data = mixed(1_000_450)
        data.fair_by_symbol["SECOND_USDT"]["fairPrice"] = "2.5"
        obs = await observe_mixed_margin_account_window(
            Reader(mixed(), data), now_ms=lambda: 1_001_000,
        )
        self.assertEqual(obs.status, "MIXED_ACCOUNT_FAIR_PRICE_DRIFT")
        self.assertNotIn("SECOND_USDT", str(obs))

    async def test_same_account_readonly_diagnostic_mixed_fallback(self):
        data = mixed()
        private = FakePrivate(data)
        public = FakePublic(data, fair_ms=1_000_400)
        # Real collector first frame has 2 clock calls, then 2 for mixed
        # first and 2 for mixed second; validation calls another 3.
        timeline = iter([
            1_000_000, 1_000_500, 1_000_700,
            1_001_000, 1_001_500, 1_001_700,
            1_002_000, 1_002_500, 1_002_700,
        ])
        result = await run_diagnostic(
            private=ReadOnlyMexcProbe(private),
            public=public,
            clock_ms=timeline.__next__,
        )
        self.assertEqual(
            result["failure_code"], "POSITION_NOT_VERIFIED_CROSS_SHORT",
        )
        self.assertEqual(result["account_cross_short_positions"], 1)
        self.assertEqual(result["account_isolated_long_positions"], 1)
        self.assertEqual(
            result["mixed_account_risk_status"],
            "MIXED_ACCOUNT_ECONOMIC_BOOKENDS_OBSERVED_NONAUTHORIZING",
        )
        self.assertEqual(result["mixed_account_positions_observed"], 2)
        self.assertEqual(result["mixed_account_cross_positions_observed"], 1)
        self.assertEqual(result["mixed_account_isolated_positions_observed"], 1)
        self.assertTrue(result["mixed_account_economic_parity_observed"])
        self.assertTrue(result["mixed_account_critical_inventory_stable"])
        self.assertFalse(result["mixed_account_cross_wallet_collateral_certified"])
        self.assertFalse(result["mixed_account_liquidation_certified"])
        self.assertFalse(result["entry_allowed"])
        self.assertFalse(result["live_authorization"])
        self.assertFalse(result["mexc_orders_sent"])
        self.assertTrue(all(method == "GET" for method, *_ in private.calls))
        self.assertNotIn("SECOND_USDT", str(result))
        self.assertNotIn("20", str(result))

    async def test_withdrawable_cash_balance_drift_is_field_named_only(self):
        second = mixed(1_000_450)
        second.assets[0]["cashBalance"] = "1999.5"
        result = await observe_mixed_margin_account_window(
            Reader(mixed(), second), now_ms=lambda: 1_001_000,
        )
        self.assertEqual(
            result.status, "MIXED_ACCOUNT_CRITICAL_INVENTORY_DRIFT",
        )
        self.assertEqual(result.changed_asset_fields, ("cashBalance",))
        self.assertEqual(result.changed_position_fields, ())
        self.assertEqual(result.changed_stop_fields, ())
        self.assertFalse(result.private_risk_schedule_changed)
        self.assertFalse(result.public_contract_terms_changed)
        self.assertFalse(result.entry_allowed)
        self.assertNotIn("1999.5", str(result))
        self.assertNotIn("TEST_USDT", str(result))

    async def test_position_margin_drift_with_accurate_account_assets_is_named(self):
        second = mixed(1_000_450)
        second.positions[1]["im"] = "31"
        second.assets[0]["positionMargin"] = "51"
        result = await observe_mixed_margin_account_window(
            Reader(mixed(), second), now_ms=lambda: 1_001_000,
        )
        self.assertEqual(
            result.status, "MIXED_ACCOUNT_CRITICAL_INVENTORY_DRIFT",
        )
        self.assertEqual(result.changed_position_fields, ("im",))
        self.assertEqual(result.changed_asset_fields, ())
        self.assertFalse(result.live_authorization)
        self.assertNotIn("31", str(result))

    async def test_risk_metadata_and_stop_change_identified_without_values(self):
        second = mixed(1_000_450)
        second.private_risk_by_symbol["TEST_USDT"][0]["mmr"] = "0.071"
        second.public_contract_by_symbol["SECOND_USDT"]["liquidationFeeRate"] = "0.08"
        second.stops[0]["stopLossPrice"] = "9.99"
        result = await observe_mixed_margin_account_window(
            Reader(mixed(), second), now_ms=lambda: 1_001_000,
        )
        self.assertEqual(
            result.status, "MIXED_ACCOUNT_CRITICAL_INVENTORY_DRIFT",
        )
        self.assertEqual(result.changed_stop_fields, ("stopLossPrice",))
        self.assertTrue(result.private_risk_schedule_changed)
        self.assertTrue(result.public_contract_terms_changed)
        self.assertEqual(result.changed_asset_fields, ())
        self.assertFalse(result.entry_allowed)
        for raw in ("SECOND_USDT", "0.071", "9.99"):
            self.assertNotIn(raw, str(result))

    async def test_read_failure_is_privacy_safe(self):
        class Failure:
            async def read(self):
                raise ValueError("PRIVATE_ACCOUNT_99999")
        obs = await observe_mixed_margin_account_window(
            Failure(), now_ms=lambda: 1_001_000,
        )
        self.assertEqual(obs.status, "MIXED_ACCOUNT_READ_OR_SCHEMA_UNVERIFIED")
        self.assertNotIn("PRIVATE_ACCOUNT", str(obs))
        self.assertFalse(obs.entry_allowed)


if __name__ == "__main__":
    unittest.main()
