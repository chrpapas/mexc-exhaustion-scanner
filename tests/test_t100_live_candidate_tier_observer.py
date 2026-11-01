"""Pure offline test of T100 candidate public/private current risk-tier witness."""
from __future__ import annotations

import copy
import unittest

from app.t100_live_candidate_tier_observer import inspect_candidate_tier
from app.t100_live_candidate_tier_observer_cli import run
from app.t100_live_readonly_probe import ReadOnlyMexcProbe
from tests.test_t100_live_incremental_tier_candidate import incr
from tests.test_t100_live_risk_tier_witness import public, private


class Response:
    def __init__(self, payload):
        self.payload = payload

    def raise_for_status(self):
        pass

    def json(self):
        return copy.deepcopy(self.payload)


class Public:
    def __init__(self, first, second=None):
        self.replies = [first, copy.deepcopy(first) if second is None else second]
        self.calls = []

    async def get(self, path, *, params):
        self.calls.append((path, dict(params)))
        return Response({
            "success": True, "code": 0,
            "data": copy.deepcopy(self.replies[len(self.calls) - 1]),
        })


class Private:
    def __init__(self, first, second=None):
        self.replies = [first, copy.deepcopy(first) if second is None else second]
        self.calls = []

    async def _private_get(self, path, params=None):
        self.calls.append((path, dict(params or {})))
        return copy.deepcopy(self.replies[len(self.calls) - 1])


class CandidateCurrentTierTests(unittest.IsolatedAsyncioTestCase):
    async def observe(self, contract=None, private_rows=None, *,
                      second_contract=None, second_private=None, lots="10"):
        public_client = Public(contract if contract is not None else incr(),
                               second_contract)
        private_client = Private(private_rows if private_rows is not None else private(),
                                 second_private)
        result = await inspect_candidate_tier(
            ReadOnlyMexcProbe(private_client), public_client,
            "TEST_USDT", proposed_contracts=lots,
        )
        return result, public_client.calls, private_client.calls

    async def test_incremental_one_tier_private_parity_and_no_authorization(self):
        r = incr()
        r.update({"riskLevelLimit": 1, "riskIncrVol": 0,
                  "riskIncrMmr": 0, "riskIncrImr": 0})
        result, pub, prv = await self.observe(contract=r)
        self.assertEqual(result.status,
                         "CANDIDATE_CURRENT_PRIVATE_TIER_COMPARED_NONAUTHORIZING")
        self.assertEqual(result.schedule_mode, "INCREASE")
        self.assertTrue(result.current_private_mmr_not_below_public)
        self.assertTrue(result.private_current_volume_cap_covers_candidate)
        self.assertTrue(result.private_cross_mode_observed)
        self.assertTrue(result.public_metadata_two_get_consistent)
        self.assertTrue(result.private_risk_two_get_consistent)
        self.assertFalse(result.future_stressed_tiers_certified)
        self.assertFalse(result.account_specific_api_order_access_certified)
        self.assertFalse(result.venue_liquidation_certified)
        self.assertFalse(result.native_stop_protection_verified)
        self.assertFalse(result.entry_allowed)
        self.assertFalse(result.live_authorization)
        self.assertFalse(result.mexc_orders_sent)
        self.assertEqual(len(pub), 2)
        self.assertEqual(len(prv), 2)
        self.assertTrue(all(x[0] == "/api/v1/contract/detail/country" for x in pub))
        self.assertTrue(all(x[0] == "/api/v1/private/account/risk_limit" for x in prv))
        self.assertNotIn("TEST_USDT", str(result))
        self.assertNotIn("100", str(result))

    async def test_custom_current_tier_parity(self):
        result, _, _ = await self.observe(contract=public())
        self.assertEqual(result.status,
                         "CANDIDATE_CURRENT_PRIVATE_TIER_COMPARED_NONAUTHORIZING")
        self.assertEqual(result.schedule_mode, "CUSTOM")
        self.assertFalse(result.live_authorization)

    async def test_api_denied_skips_private_reads(self):
        r = incr()
        r["apiAllowed"] = False
        result, _, prv = await self.observe(contract=r)
        self.assertEqual(result.status, "CANDIDATE_TIER_PUBLIC_INELIGIBLE")
        self.assertEqual(len(prv), 0)

    async def test_metadata_drift_skips_private_reads(self):
        r = incr()
        changed = copy.deepcopy(r)
        changed["riskBaseVol"] += 1
        result, _, prv = await self.observe(contract=r, second_contract=changed)
        self.assertEqual(result.status, "CANDIDATE_TIER_PUBLIC_METADATA_DRIFT")
        self.assertEqual(prv, [])

    async def test_private_risk_drift_fails_closed(self):
        first = private()
        later = copy.deepcopy(first)
        later["TEST_USDT"][0]["mmr"] = 0.02
        result, _, prv = await self.observe(
            private_rows=first, second_private=later,
        )
        self.assertEqual(result.status, "CANDIDATE_TIER_PRIVATE_RISK_DRIFT")
        self.assertEqual(len(prv), 2)

    async def test_manual_isolated_mode_not_assumed_cross(self):
        risk = private()
        risk["TEST_USDT"][0]["openType"] = 1
        result, _, _ = await self.observe(private_rows=risk)
        self.assertEqual(result.status, "CANDIDATE_TIER_PRIVATE_CROSS_NOT_OBSERVED")
        self.assertFalse(result.private_cross_mode_observed)

    async def test_private_mmr_below_public_rejected(self):
        risk = private()
        risk["TEST_USDT"][0]["mmr"] = 0.005
        risk["TEST_USDT"][0]["currentMmr"] = 0.005
        result, _, _ = await self.observe(private_rows=risk)
        self.assertEqual(result.status, "CANDIDATE_TIER_PRIVATE_MMR_BELOW_PUBLIC")

    async def test_private_size_cap_blocks(self):
        result, _, _ = await self.observe(lots="101")
        self.assertEqual(result.status, "CANDIDATE_TIER_PRIVATE_CAP_BELOW_CANDIDATE")
        self.assertFalse(result.live_authorization)

    async def test_prospective_higher_tier_not_current_certified(self):
        risk = private()
        risk["TEST_USDT"][0]["maxVol"] = 300
        result, _, _ = await self.observe(private_rows=risk, lots="101")
        self.assertEqual(result.status,
                         "CANDIDATE_TIER_PROSPECTIVE_LEVEL_RECHECK_REQUIRED")
        self.assertTrue(result.higher_tier_requires_recheck)
        self.assertFalse(result.future_stressed_tiers_certified)

    async def test_missing_private_short_side(self):
        risk = private()
        risk["TEST_USDT"][0]["positionType"] = 1
        result, _, _ = await self.observe(private_rows=risk)
        self.assertEqual(result.status, "CANDIDATE_TIER_PRIVATE_SHORT_UNVERIFIED")

    async def test_invalid_quantity_prevents_network(self):
        pub, prv = Public(incr()), Private(private())
        for value in ("NaN", "0", "-1", "inf", "SECRET"):
            result = await inspect_candidate_tier(
                ReadOnlyMexcProbe(prv), pub, "TEST_USDT", proposed_contracts=value,
            )
            self.assertEqual(result.status, "CANDIDATE_TIER_QUANTITY_INVALID")
        self.assertEqual(pub.calls, [])
        self.assertEqual(prv.calls, [])

    async def test_cli_requires_paper_and_kill_without_importing_client(self):
        for env in ({}, {
            "T100_EXECUTION_MODE": "live",
            "T100_LIVE_ORDER_API_ENABLED": "false",
            "T100_LIVE_KILL_SWITCH": "true",
        }):
            result = await run("TEST_USDT", "10", environ=env)
            self.assertEqual(result["status"], "CANDIDATE_TIER_PAPER_KILL_REQUIRED")
            self.assertFalse(result["mexc_orders_sent"])
        guarded = {
            "T100_EXECUTION_MODE": "paper",
            "T100_LIVE_ORDER_API_ENABLED": "false",
            "T100_LIVE_KILL_SWITCH": "true",
        }
        result = await run("TEST_USDT", "10", environ=guarded)
        self.assertEqual(result["status"], "CANDIDATE_TIER_CREDENTIALS_MISSING")


if __name__ == "__main__":
    unittest.main()
