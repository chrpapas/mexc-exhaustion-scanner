"""Offline current-country API-eligibility census, no venue network or orders."""
from __future__ import annotations

import copy
import unittest

from app.t100_live_api_eligibility_census import (
    COUNTRY_CONTRACT_ENDPOINT, inspect_country_api_census,
    summarize_country_api_contracts,
)
from app.t100_live_api_eligibility_census_cli import run


def row(symbol, **updates):
    base = {
        "symbol": symbol,
        "state": 0,
        "futureType": 1,
        "positionOpenType": 3,
        "quoteCoin": "USDT",
        "settleCoin": "USDT",
        "apiAllowed": True,
        "riskLimitMode": "INCREASE",
        "riskLimitType": "BY_VOLUME",
        "riskLevelLimit": 1,
        "riskIncrVol": 0,
        "maintenanceMarginRate": 0.01,
    }
    base.update(updates)
    return base


def snapshot():
    return {
        "success": True, "code": 0,
        "data": [
            row("AAA_USDT"),
            row("BBB_USDT", riskLevelLimit=3),
            row("CCC_USDT", apiAllowed=False),
            row("DDD_USDT", apiAllowed=None),
            row("EEE_USDT", riskLimitMode="CUSTOM",
                riskLimitCustom=[{"level": 1}, {"level": 2}]),
            row("FFF_USDT", riskLimitType="BY_VALUE"),
            row("GGG_USDT", state=4),
            row("HHH_USDC", quoteCoin="USDC", settleCoin="USDC"),
            row("III_USDT", riskLevelLimit=None),
            row("JJJ_USDT", riskLimitMode="CUSTOM",
                riskLimitCustom=[{"level": 1}]),
        ],
    }


class Response:
    def __init__(self, payload):
        self.payload = payload

    def raise_for_status(self):
        return None

    def json(self):
        return copy.deepcopy(self.payload)


class PublicClient:
    def __init__(self, first, second, fail_second=False):
        self.data = [first, second]
        self.calls = []
        self.fail_second = fail_second

    async def get(self, path, *, headers):
        self.calls.append((path, dict(headers)))
        if self.fail_second and len(self.calls) == 2:
            raise RuntimeError("DO_NOT_LEAK_PUBLIC_URL")
        return Response(self.data[len(self.calls)-1])


class CensusTests(unittest.IsolatedAsyncioTestCase):
    def test_counts_are_current_only_never_execution_permission(self):
        first, second = snapshot(), snapshot()
        second["data"].reverse()
        result = summarize_country_api_contracts(first, second)
        self.assertEqual(
            result.status,
            "COUNTRY_API_CENSUS_CURRENT_PUBLIC_COUNTS_NONAUTHORIZING",
        )
        self.assertEqual(result.returned_contract_rows, 10)
        self.assertEqual(result.usdt_perpetual_cross_supported, 8)
        self.assertEqual(result.api_allowed_now, 6)
        self.assertEqual(result.api_denied_now, 1)
        self.assertEqual(result.api_permission_unknown, 1)
        self.assertEqual(result.api_allowed_incremental_by_volume, 3)
        self.assertEqual(result.api_allowed_incremental_one_tier, 1)
        self.assertEqual(result.api_allowed_incremental_multi_tier, 1)
        self.assertEqual(result.api_allowed_incremental_levels_unknown, 1)
        self.assertEqual(result.api_allowed_custom_by_volume, 2)
        self.assertEqual(result.api_allowed_custom_multi_tier, 1)
        self.assertEqual(result.api_allowed_unsupported_or_unknown_mode, 1)
        self.assertTrue(result.country_metadata_two_get_consistent)
        self.assertFalse(result.historical_api_eligibility_certified)
        self.assertFalse(result.account_specific_api_order_access_certified)
        self.assertFalse(result.liquidity_or_capacity_certified)
        self.assertFalse(result.future_stressed_tiers_certified)
        self.assertFalse(result.venue_liquidation_certified)
        self.assertFalse(result.entry_allowed)
        self.assertFalse(result.live_authorization)
        self.assertNotIn("AAA_USDT", str(result))
        self.assertNotIn("BBB_USDT", str(result))

    def test_public_metadata_drifts_no_counts_claimed(self):
        first = snapshot()
        second = copy.deepcopy(first)
        second["data"][1]["apiAllowed"] = False
        result = summarize_country_api_contracts(first, second)
        self.assertEqual(result.status, "COUNTRY_API_CENSUS_METADATA_DRIFT")
        self.assertIsNone(result.api_allowed_now)
        self.assertFalse(result.country_metadata_two_get_consistent)

    def test_bad_or_singleton_schema_never_claims_global_coverage(self):
        valid = snapshot()
        malformed_cases = (
            {"success": True, "code": 0, "data": {"symbol": "AAA_USDT"}},
            {"success": True, "code": 0, "data": [row("AAA_USDT")]},
            {"success": True, "code": 0, "data": [
                row("AAA_USDT"), row("AAA_USDT"),
            ]},
            {"success": True, "code": 0, "data": [
                row("AAA_USDT"), {"symbol": "SECRET_WEIRD_SYMBOL"},
            ]},
            {"success": False, "code": 900, "message": "SECRET_CODE"},
            {"success": True, "code": 0, "data": None},
        )
        for payload in malformed_cases:
            result = summarize_country_api_contracts(payload, valid)
            self.assertEqual(result.status, "COUNTRY_API_CENSUS_SCHEMA_UNVERIFIED")
            self.assertIsNone(result.returned_contract_rows)
            self.assertFalse(result.entry_allowed)
            self.assertNotIn("SECRET", str(result))

    async def test_exactly_two_public_gets_without_auth_headers_or_orders(self):
        data = snapshot()
        client = PublicClient(data, data)
        result = await inspect_country_api_census(client)
        self.assertEqual(
            result.status,
            "COUNTRY_API_CENSUS_CURRENT_PUBLIC_COUNTS_NONAUTHORIZING",
        )
        self.assertEqual(len(client.calls), 2)
        self.assertTrue(all(call[0] == COUNTRY_CONTRACT_ENDPOINT for call in client.calls))
        self.assertTrue(all("ApiKey" not in call[1] for call in client.calls))
        self.assertFalse(result.mexc_orders_sent)

    async def test_read_failure_halts_no_extra_get(self):
        data = snapshot()
        client = PublicClient(data, data, fail_second=True)
        result = await inspect_country_api_census(client)
        self.assertEqual(result.status, "COUNTRY_API_CENSUS_PUBLIC_GET_FAILED")
        self.assertEqual(len(client.calls), 2)
        self.assertNotIn("DO_NOT_LEAK", str(result))

    async def test_paper_guard_blocks_before_loading_http_library(self):
        for env, status in (
            ({}, "COUNTRY_API_CENSUS_PAPER_KILL_REQUIRED"),
            ({
                "T100_EXECUTION_MODE": "live",
                "T100_LIVE_ORDER_API_ENABLED": "false",
                "T100_LIVE_KILL_SWITCH": "true",
            }, "COUNTRY_API_CENSUS_PAPER_KILL_REQUIRED"),
        ):
            result = await run(environ=env)
            self.assertEqual(result["status"], status)
            self.assertFalse(result["entry_allowed"])
            self.assertFalse(result["mexc_orders_sent"])


if __name__ == "__main__":
    unittest.main()
