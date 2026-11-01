"""Offline, no-network tests for public-only V40 tier research and GET audit."""
from __future__ import annotations

import copy
import unittest
from decimal import Decimal as D

from app.t100_live_public_v40_tier_stress import assess_public_v40_tier_stress
from app.t100_live_public_v40_tier_stress_cli import inspect_public_tier_scenario, run
from tests.test_t100_live_incremental_tier_candidate import incr
from tests.test_t100_live_risk_tier_witness import public


class Response:
    def __init__(self, row):
        self.row = row

    def raise_for_status(self):
        pass

    def json(self):
        return {"success": True, "code": 0, "data": copy.deepcopy(self.row)}


class Public:
    def __init__(self, first, second=None, *, fail=False):
        self.rows = [first, copy.deepcopy(first) if second is None else second]
        self.calls = []
        self.fail = fail

    async def get(self, path, *, params):
        self.calls.append((path, dict(params)))
        if self.fail:
            raise OSError("UNSAFE_PUBLIC_RESPONSE_URL")
        return Response(self.rows[len(self.calls) - 1])


class PublicV40ResearchTests(unittest.TestCase):
    def test_incremental_worst_published_rate_and_stressed_notional(self):
        r = assess_public_v40_tier_stress(
            incr(), contracts="50", entry_reference_price="2",
        )
        self.assertEqual(r.status, "PUBLIC_V40_VOLUME_TIER_SCENARIO_NONAUTHORIZING")
        self.assertEqual(r.risk_limit_mode, "INCREASE")
        self.assertEqual(r.risk_limit_type, "BY_VOLUME")
        self.assertEqual(r.published_tier_count, 3)
        self.assertEqual(r.public_research_maintenance_rate, D("0.03"))
        self.assertEqual(r.entry_reference_notional_usdt, D("200"))
        self.assertEqual(r.research_stop_gap_notional_usdt, D("360"))
        self.assertEqual(r.research_published_maintenance_reserve_usdt, D("10.8"))
        self.assertEqual(r.research_published_liquidation_fee_reserve_usdt, D("1.800"))
        self.assertFalse(r.public_tiers_are_future_upper_bound)
        self.assertFalse(r.private_current_cross_verified)
        self.assertFalse(r.future_stressed_tiers_certified)
        self.assertFalse(r.account_collateral_certified)
        self.assertFalse(r.venue_liquidation_certified)
        self.assertFalse(r.native_stop_protection_verified)
        self.assertFalse(r.entry_allowed)
        self.assertFalse(r.live_authorization)
        self.assertFalse(r.mexc_orders_sent)

    def test_custom_volume_tier_research_calculates_max_not_selected_mmr(self):
        r = assess_public_v40_tier_stress(
            public(), contracts="250", entry_reference_price="1",
        )
        self.assertEqual(r.status, "PUBLIC_V40_VOLUME_TIER_SCENARIO_NONAUTHORIZING")
        self.assertEqual(r.risk_limit_mode, "CUSTOM")
        self.assertEqual(r.published_tier_count, 3)
        self.assertEqual(r.public_research_maintenance_rate, D("0.08"))
        self.assertEqual(r.research_stop_gap_notional_usdt, D("900.00"))
        self.assertEqual(r.research_published_maintenance_reserve_usdt, D("72.0000"))

    def test_by_value_or_unknown_mode_cannot_be_treated_as_volume(self):
        for attr, val in (("riskLimitType", "BY_VALUE"),
                          ("riskLimitMode", "FUTURE"),
                          ("riskLimitType", None)):
            p = incr()
            p[attr] = val
            r = assess_public_v40_tier_stress(
                p, contracts=50, entry_reference_price=1,
            )
            self.assertEqual(r.status, "PUBLIC_V40_UNSUPPORTED_RISK_MODE")
            self.assertFalse(r.published_tiers_valid)

    def test_api_denied_or_no_cross_support_blocks(self):
        for key, value in (("apiAllowed", False), ("positionOpenType", 1),
                           ("state", 4), ("settleCoin", "BTC")):
            p = incr()
            p[key] = value
            r = assess_public_v40_tier_stress(
                p, contracts=50, entry_reference_price=1,
            )
            self.assertEqual(r.status, "PUBLIC_V40_CONTRACT_INELIGIBLE")
            self.assertFalse(r.entry_allowed)

    def test_incremental_short_only_fields_are_not_invented(self):
        p = incr()
        p["riskLongShortSwitch"] = 1
        p["riskBaseVolShort"] = 55
        p["riskIncrVolShort"] = 20
        success = assess_public_v40_tier_stress(
            p, contracts=75, entry_reference_price=1,
        )
        self.assertEqual(success.status, "PUBLIC_V40_VOLUME_TIER_SCENARIO_NONAUTHORIZING")
        del p["riskBaseVolShort"]
        fail = assess_public_v40_tier_stress(
            p, contracts=75, entry_reference_price=1,
        )
        self.assertEqual(fail.status, "PUBLIC_V40_INCREMENTAL_SCHEMA_OR_QUANTITY_BLOCKED")

    def test_quantity_cap_and_custom_tier_incoherence(self):
        p = public()
        self.assertEqual(
            assess_public_v40_tier_stress(
                p, contracts=1001, entry_reference_price=1,
            ).status, "PUBLIC_V40_QUANTITY_EXCEEDS_PUBLISHED_TIERS",
        )
        p["riskLimitCustom"][1]["mmr"] = 0.001
        self.assertEqual(
            assess_public_v40_tier_stress(
                p, contracts=50, entry_reference_price=1,
            ).status, "PUBLIC_V40_RISK_SCHEDULE_UNVERIFIED",
        )

    def test_nonfinite_or_invalid_quantities_and_leverage_block(self):
        for contracts, price, leverage in (
            ("NaN", 1, 1), (0, 1, 1), (-1, 1, 1),
            (50, "inf", 1), (50, 0, 1), (50, 1, True),
            (50, 1, 3),
        ):
            with self.subTest(contracts=contracts, price=price, leverage=leverage):
                result = assess_public_v40_tier_stress(
                    incr(), contracts=contracts,
                    entry_reference_price=price, leverage=leverage,
                )
                self.assertNotEqual(
                    result.status, "PUBLIC_V40_VOLUME_TIER_SCENARIO_NONAUTHORIZING",
                )
                self.assertFalse(result.entry_allowed)

    def test_invalid_public_liquidation_fee_or_max_leverage_block(self):
        for field, value in (("liquidationFeeRate", "NaN"),
                             ("liquidationFeeRate", "1.4"),
                             ("maxLeverage", 0),
                             ("countryConfigContractMaxLeverage", True)):
            p = incr()
            p[field] = value
            r = assess_public_v40_tier_stress(
                p, contracts=50, entry_reference_price=1,
            )
            self.assertEqual(r.status, "PUBLIC_V40_RISK_SCHEDULE_UNVERIFIED")

    def test_no_connection_to_live_transport_code(self):
        import inspect
        import app.t100_live_public_v40_tier_stress as module
        import app.t100_live_mexc_adapter as adapter
        text = inspect.getsource(module)
        for prohibited in ("._private_post(", "MexcTradeClient(", "httpx.",
                           "LiveMutationPermit", "asyncpg.", ".send_one("):
            self.assertNotIn(prohibited, text)
        self.assertNotIn("t100_live_public_v40_tier_stress",
                         inspect.getsource(adapter))


class PublicV40ResearchCliTests(unittest.IsolatedAsyncioTestCase):
    async def test_double_public_get_and_only_allowlisted_redacted_fields(self):
        client = Public(incr())
        result = await inspect_public_tier_scenario(
            client, "TEST_USDT", contracts="50",
            entry_reference_price="2", leverage=1,
        )
        self.assertEqual(result["status"], "PUBLIC_V40_VOLUME_TIER_SCENARIO_NONAUTHORIZING")
        self.assertEqual(len(client.calls), 2)
        self.assertEqual(client.calls, [
            ("/api/v1/contract/detail/country", {"symbol": "TEST_USDT"}),
            ("/api/v1/contract/detail/country", {"symbol": "TEST_USDT"}),
        ])
        self.assertTrue(result["published_research_mmr_observed"])
        self.assertTrue(result["public_maintenance_reserve_research_computed"])
        self.assertFalse(result["future_stressed_tiers_certified"])
        self.assertFalse(result["account_collateral_certified"])
        self.assertFalse(result["live_authorization"])
        for private_value in ("TEST_USDT", "360", "200", "10.8"):
            self.assertNotIn(private_value, str(result))

    async def test_drift_rejected_before_research(self):
        p = incr()
        changed = copy.deepcopy(p)
        changed["riskIncrVol"] = 50
        client = Public(p, changed)
        r = await inspect_public_tier_scenario(
            client, "TEST_USDT", contracts="50",
            entry_reference_price="2", leverage=1,
        )
        self.assertEqual(r["status"], "PUBLIC_V40_METADATA_DRIFT")
        self.assertFalse(r["entry_allowed"])

    async def test_public_get_error_redacted(self):
        r = await inspect_public_tier_scenario(
            Public(incr(), fail=True), "TEST_USDT", contracts="50",
            entry_reference_price="2", leverage=1,
        )
        self.assertEqual(r["status"], "PUBLIC_V40_GET_FAILED")
        self.assertNotIn("UNSAFE_PUBLIC_RESPONSE_URL", str(r))

    async def test_invalid_symbol_never_accesses_public_api(self):
        client = Public(incr())
        r = await inspect_public_tier_scenario(
            client, "../MEXC_API_SECRET", contracts="50",
            entry_reference_price="2", leverage=1,
        )
        self.assertEqual(r["status"], "PUBLIC_V40_SYMBOL_UNVERIFIED")
        self.assertEqual(client.calls, [])

    async def test_paper_kill_controls_checked_before_any_get(self):
        client = Public(incr())
        for env in ({}, {"T100_EXECUTION_MODE": "live",
                         "T100_LIVE_ORDER_API_ENABLED": "false",
                         "T100_LIVE_KILL_SWITCH": "true"}):
            r = await run("TEST_USDT", contracts="50",
                          entry_reference_price="2", public=client, environ=env)
            self.assertEqual(r["status"], "PUBLIC_V40_PAPER_KILL_REQUIRED")
        self.assertEqual(client.calls, [])
        guarded = {"T100_EXECUTION_MODE": "paper",
                   "T100_LIVE_ORDER_API_ENABLED": "false",
                   "T100_LIVE_KILL_SWITCH": "true"}
        r = await run("TEST_USDT", contracts="50",
                      entry_reference_price="2", public=client, environ=guarded)
        self.assertEqual(r["status"], "PUBLIC_V40_VOLUME_TIER_SCENARIO_NONAUTHORIZING")
        self.assertEqual(len(client.calls), 2)


if __name__ == "__main__":
    unittest.main()
