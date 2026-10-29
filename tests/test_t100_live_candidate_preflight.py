"""Offline tests for read-only candidate preflight: never issue real HTTP or orders."""
import unittest

from app.t100_live_candidate_preflight_cli import inspect_candidate, run
from app.t100_live_readonly_probe import ReadOnlyMexcProbe
from tests.test_t100_live_cross_risk_collector import FakePrivate, FakePublic
from tests.test_t100_live_cross_risk_observation import frame


def example():
    x = frame()
    d = x.public_contract_by_symbol["TEST_USDT"]
    d.update({
        "riskLimitMode": "INCREASE", "riskLimitType": "BY_VOLUME",
        "riskLongShortSwitch": 0, "riskBaseVol": 100, "riskIncrVol": 100,
        "riskLevelLimit": 3, "maintenanceMarginRate": 0.01,
        "initialMarginRate": 0.02, "riskIncrMmr": 0.01, "riskIncrImr": 0.01,
    })
    return x


class CandidatePreflightTests(unittest.IsolatedAsyncioTestCase):
    async def test_metadata_observed_never_authorizes(self):
        x = example()
        private = FakePrivate(x)
        public = FakePublic(x)
        result = await inspect_candidate(ReadOnlyMexcProbe(private), public, "TEST_USDT")
        self.assertEqual(result.status, "CANDIDATE_METADATA_OBSERVED_NONAUTHORIZING")
        self.assertTrue(result.private_short_tier_observed)
        self.assertTrue(result.public_metadata_two_get_consistent)
        self.assertEqual(result.risk_schedule_mode, "INCREASE")
        self.assertFalse(result.entry_allowed)
        self.assertFalse(result.live_authorization)
        self.assertFalse(result.account_collateral_certified)
        self.assertTrue(result.manual_positions_ignored)
        self.assertEqual(len(public.calls), 2)
        self.assertEqual(len(private.calls), 1)
        self.assertTrue(all(c[0] == "GET" for c in public.calls + private.calls))
        self.assertNotIn("TEST_USDT", str(result))

    async def test_isolated_only_rejected_without_private_calls(self):
        x = example()
        x.public_contract_by_symbol["TEST_USDT"]["positionOpenType"] = 1
        private = FakePrivate(x)
        result = await inspect_candidate(ReadOnlyMexcProbe(private), FakePublic(x), "TEST_USDT")
        self.assertEqual(result.status, "CONTRACT_NOT_ELIGIBLE")
        self.assertEqual(result.cross_margin_support, "ISOLATED_ONLY")
        self.assertFalse(result.live_authorization)
        self.assertEqual(private.calls, [])

    async def test_invalid_symbol_never_queries_venue(self):
        x = example()
        private = FakePrivate(x)
        public = FakePublic(x)
        result = await inspect_candidate(ReadOnlyMexcProbe(private), public, "../bad")
        self.assertEqual(result.status, "INVALID_CANDIDATE_SYMBOL")
        self.assertEqual(private.calls, [])
        self.assertEqual(public.calls, [])

    async def test_kill_switch_fail_closed_before_network(self):
        result = await run("TEST_USDT", environ={})
        self.assertEqual(result.status, "PAPER_KILL_REQUIRED")
        self.assertFalse(result.entry_allowed)

    async def test_missing_credentials_fail_closed(self):
        result = await run("TEST_USDT", environ={
            "T100_EXECUTION_MODE": "paper",
            "T100_LIVE_ORDER_API_ENABLED": "false",
            "T100_LIVE_KILL_SWITCH": "true",
        })
        self.assertEqual(result.status, "CREDENTIALS_MISSING")

    async def test_unsupported_schedule_blocks(self):
        x = example()
        x.public_contract_by_symbol["TEST_USDT"]["riskLimitType"] = "BY_VALUE"
        result = await inspect_candidate(
            ReadOnlyMexcProbe(FakePrivate(x)), FakePublic(x), "TEST_USDT"
        )
        self.assertEqual(result.status, "RISK_SCHEDULE_UNSUPPORTED")
        self.assertFalse(result.entry_allowed)


if __name__ == "__main__":
    unittest.main()
