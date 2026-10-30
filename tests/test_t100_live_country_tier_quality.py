"""Offline tests for redacted public volume-tier structural quality census."""
from __future__ import annotations

import copy
import unittest

from app.t100_live_country_tier_quality import (
    assess_country_tier_quality, inspect_country_tier_quality,
)


def incremental(symbol: str = "AAA_USDT", **updates):
    r = {
        "symbol": symbol, "state": 0, "apiAllowed": True,
        "positionOpenType": 3, "futureType": 1,
        "quoteCoin": "USDT", "settleCoin": "USDT",
        "riskLimitMode": "INCREASE", "riskLimitType": "BY_VOLUME",
        "riskLevelLimit": 1, "riskLongShortSwitch": 0,
        "riskBaseVol": 100, "riskIncrVol": 0,
        "riskIncrMmr": 0, "riskIncrImr": 0,
        "maintenanceMarginRate": "0.01", "initialMarginRate": "0.02",
        "contractSize": 1, "liquidationFeeRate": "0.002",
        "minLeverage": 1, "maxLeverage": 100,
        "countryConfigContractMaxLeverage": 0,
    }
    r.update(updates)
    return r


def custom(symbol: str = "BBB_USDT", **updates):
    r = incremental(symbol)
    r.update({
        "riskLimitMode": "CUSTOM",
        "riskLimitCustom": [
            {"level": 1, "maxVol": 100, "mmr": "0.01", "imr": "0.02", "maxLeverage": 50},
            {"level": 2, "maxVol": 200, "mmr": "0.02", "imr": "0.04", "maxLeverage": 25},
        ],
    })
    r.update(updates)
    return r


def envelope(*rows):
    return {"success": True, "code": 0, "data": list(rows)}


class Response:
    def __init__(self, payload):
        self.payload = payload

    def raise_for_status(self):
        pass

    def json(self):
        return copy.deepcopy(self.payload)


class Client:
    def __init__(self, first, second, fail_second=False):
        self.payloads = [first, second]
        self.calls = []
        self.fail_second = fail_second

    async def get(self, path, *, headers):
        self.calls.append((path, headers))
        if self.fail_second and len(self.calls) == 2:
            raise ValueError("LEAK_SECRET")
        return Response(self.payloads[len(self.calls) - 1])


class CountryTierQualityTests(unittest.IsolatedAsyncioTestCase):
    def test_both_schedule_families_are_classified_not_authorized(self):
        first = envelope(incremental(), custom())
        second = copy.deepcopy(first)
        second["data"].reverse()
        result = assess_country_tier_quality(first, second)
        self.assertEqual(
            result.status, "COUNTRY_TIER_QUALITY_CURRENT_PUBLIC_SHAPES_NONAUTHORIZING"
        )
        self.assertEqual(result.api_allowed_cross_usdt_perpetual, 2)
        self.assertEqual(result.incremental_volume_shapes_valid, 1)
        self.assertEqual(result.custom_volume_shapes_valid, 1)
        self.assertEqual(result.incremental_volume_shapes_invalid, 0)
        self.assertEqual(result.custom_volume_shapes_invalid, 0)
        self.assertTrue(result.public_snapshots_consistent)
        self.assertFalse(result.account_specific_api_order_access_certified)
        self.assertFalse(result.future_stressed_tiers_certified)
        self.assertFalse(result.venue_liquidation_certified)
        self.assertFalse(result.entry_allowed)
        self.assertFalse(result.live_authorization)
        self.assertFalse(result.mexc_orders_sent)
        self.assertNotIn("AAA_USDT", str(result))

    def test_invalid_incremental_never_counted_valid(self):
        cases = [
            {"riskBaseVol": 0},
            {"riskLongShortSwitch": 1},  # missing short-specific volume
            {"riskLevelLimit": 2, "riskIncrVol": 0},
            {"riskLevelLimit": 2, "riskIncrVol": 10, "riskIncrMmr": "0.99"},
            {"maintenanceMarginRate": "NaN"},
            {"riskLevelLimit": True},
            {"initialMarginRate": "0.001"},
            {"minLeverage": 50, "maxLeverage": 2},
            {"liquidationFeeRate": 1},
        ]
        for changes in cases:
            with self.subTest(changes=changes):
                data = envelope(incremental(**changes), custom())
                outcome = assess_country_tier_quality(data, data)
                self.assertEqual(outcome.incremental_volume_shapes_invalid, 1)
                self.assertEqual(outcome.incremental_volume_shapes_valid, 0)
                self.assertFalse(outcome.entry_allowed)

    def test_incremental_failure_reasons_are_redacted_and_reconciled(self):
        cases = [
            ({"riskBaseVol": 0}, "VOLUME_FIELDS_UNVERIFIED"),
            ({"riskLongShortSwitch": 1}, "VOLUME_FIELDS_UNVERIFIED"),
            ({"riskLevelLimit": 2, "riskIncrVol": 0}, "TIER_STEP_INCOHERENT"),
            ({"riskLevelLimit": 2, "riskIncrVol": 10,
              "riskIncrMmr": "0.99"}, "MARGIN_RATE_SCHEDULE_INCOHERENT"),
            ({"maintenanceMarginRate": "NaN"}, "MARGIN_RATE_FIELDS_UNVERIFIED"),
            ({"riskLevelLimit": True}, "LEVEL_OR_SIDE_UNVERIFIED"),
            ({"initialMarginRate": "0.001"}, "MARGIN_RATE_SCHEDULE_INCOHERENT"),
            ({"minLeverage": 50, "maxLeverage": 2}, "LEVERAGE_FIELDS_UNVERIFIED"),
            ({"liquidationFeeRate": 1}, "LIQUIDATION_FEE_INCOHERENT"),
            ({"contractSize": 0}, "CONTRACT_OR_FEE_FIELDS_UNVERIFIED"),
        ]
        for changes, expected in cases:
            with self.subTest(expected=expected, changes=changes):
                data = envelope(incremental(**changes), custom())
                result = assess_country_tier_quality(data, data)
                self.assertEqual(result.incremental_volume_shapes_invalid, 1)
                self.assertEqual(result.incremental_invalid_reasons, {expected: 1})
                self.assertFalse(result.live_authorization)
                self.assertNotIn("AAA_USDT", str(result))
        first = envelope(incremental("AAA_USDT", riskBaseVol=0),
                         incremental("CCC_USDT", riskBaseVol=0),
                         custom())
        result = assess_country_tier_quality(first, first)
        self.assertEqual(result.incremental_volume_shapes_invalid, 2)
        self.assertEqual(
            result.incremental_invalid_reasons, {"VOLUME_FIELDS_UNVERIFIED": 2},
        )
        self.assertEqual(sum(result.incremental_invalid_reasons.values()), 2)

    def test_short_specific_and_incremented_volume(self):
        r = incremental(
            riskLongShortSwitch=1, riskBaseVolShort=60, riskIncrVolShort=40,
            riskLevelLimit=3, riskIncrMmr="0.01", riskIncrImr="0.01",
        )
        result = assess_country_tier_quality(envelope(r, custom()), envelope(r, custom()))
        self.assertEqual(result.incremental_volume_shapes_valid, 1)

    def test_custom_boundary_failures(self):
        cases = [
            [{"level": 1, "maxVol": 100, "mmr": ".01", "imr": ".02", "maxLeverage": 50},
             {"level": 2, "maxVol": 100, "mmr": ".02", "imr": ".04", "maxLeverage": 25}],
            [{"level": 1, "maxVol": 100, "mmr": ".01", "imr": ".02", "maxLeverage": 50},
             {"level": 3, "maxVol": 200, "mmr": ".02", "imr": ".04", "maxLeverage": 25}],
            [{"level": 1, "maxVol": 100, "mmr": ".01", "imr": ".02", "maxLeverage": 50},
             {"level": 2, "maxVol": 200, "mmr": ".005", "imr": ".04", "maxLeverage": 25}],
            [{"level": 1, "maxVol": 100, "mmr": ".01", "imr": ".02", "maxLeverage": 50},
             {"level": 2, "maxVol": 200, "mmr": ".02", "imr": ".04", "maxLeverage": 100}],
        ]
        for tiers in cases:
            with self.subTest(tiers=tiers):
                data = envelope(incremental(), custom(riskLimitCustom=tiers))
                out = assess_country_tier_quality(data, data)
                self.assertEqual(out.custom_volume_shapes_invalid, 1)

    def test_unknown_api_denied_not_mistaken_for_valid(self):
        data = envelope(
            incremental(apiAllowed=False), custom(apiAllowed=None),
            incremental("CCC_USDT", riskLimitType="BY_VALUE"),
        )
        out = assess_country_tier_quality(data, data)
        self.assertEqual(out.api_allowed_cross_usdt_perpetual, 1)
        self.assertEqual(out.unsupported_or_unknown_shapes, 1)
        self.assertEqual(out.incremental_volume_shapes_valid, 0)

    def test_metadata_drift_censors_all_counts(self):
        first = envelope(incremental(), custom())
        second = copy.deepcopy(first)
        second["data"][0]["riskBaseVol"] = 200
        out = assess_country_tier_quality(first, second)
        self.assertEqual(out.status, "COUNTRY_TIER_QUALITY_PUBLIC_METADATA_DRIFT")
        self.assertIsNone(out.incremental_volume_shapes_valid)
        self.assertFalse(out.public_snapshots_consistent)

    def test_unknown_or_duplicate_schema_censors_all_counts(self):
        data = envelope(incremental(), custom())
        for bad in [
            {"success": False, "code": 123, "data": data["data"]},
            envelope(incremental()),
            envelope(incremental(), incremental()),
        ]:
            out = assess_country_tier_quality(bad, data)
            self.assertEqual(out.status, "COUNTRY_TIER_QUALITY_PUBLIC_SCHEMA_UNVERIFIED")
            self.assertIsNone(out.api_allowed_cross_usdt_perpetual)

    async def test_public_only_two_gets_and_fail_closed(self):
        data = envelope(incremental(), custom())
        client = Client(data, data)
        out = await inspect_country_tier_quality(client)
        self.assertEqual(out.incremental_volume_shapes_valid, 1)
        self.assertEqual(len(client.calls), 2)
        self.assertTrue(all(c[0] == "/api/v1/contract/detail/country" for c in client.calls))
        self.assertTrue(all("ApiKey" not in c[1] for c in client.calls))
        failed = Client(data, data, fail_second=True)
        out = await inspect_country_tier_quality(failed)
        self.assertEqual(out.status, "COUNTRY_TIER_QUALITY_PUBLIC_GET_FAILED")
        self.assertNotIn("LEAK_SECRET", str(out))
        self.assertEqual(len(failed.calls), 2)

    async def test_cli_rejects_unarmed_environment(self):
        from app.t100_live_country_tier_quality_cli import run
        for env in ({}, {
            "T100_EXECUTION_MODE": "live",
            "T100_LIVE_ORDER_API_ENABLED": "false",
            "T100_LIVE_KILL_SWITCH": "true",
        }):
            outcome = await run(environ=env)
            self.assertEqual(
                outcome["status"], "COUNTRY_TIER_QUALITY_PAPER_KILL_REQUIRED",
            )
            self.assertFalse(outcome["entry_allowed"])
            self.assertFalse(outcome["mexc_orders_sent"])


if __name__ == "__main__":
    unittest.main()
