"""Offline MEXC INCREASE tier candidate probe: no HTTP, no orders."""
import unittest

from app.t100_live_incremental_tier_probe import inspect_incremental_tier_candidate
from app.t100_live_incremental_tier_probe_cli import run
from app.t100_live_readonly_probe import ReadOnlyMexcProbe
from tests.test_t100_live_cross_risk_collector import FakePrivate, FakePublic
from tests.test_t100_live_cross_risk_observation import frame


def data():
    x = frame()
    x.public_contract_by_symbol["TEST_USDT"].update({
        "riskLimitMode": "INCREASE",
        "riskLimitType": "BY_VOLUME",
        "riskLongShortSwitch": 0,
        "riskBaseVol": 100, "riskIncrVol": 100,
        "riskLevelLimit": 3, "maintenanceMarginRate": 0.01,
        "initialMarginRate": 0.02,
        "riskIncrMmr": 0.01, "riskIncrImr": 0.01,
    })
    return x


class ProbeTests(unittest.IsolatedAsyncioTestCase):
    async def test_private_current_exact_match_non_authorizing(self):
        x = data()
        private = FakePrivate(x)
        public = FakePublic(x)
        r = await inspect_incremental_tier_candidate(ReadOnlyMexcProbe(private), public)
        self.assertEqual(r.status, "INCR_PROBE_PUBLIC_CANDIDATE_PRIVATE_COMPARED_NONAUTHORIZING")
        self.assertEqual(r.private_current_mmr_exact_match_count, 1)
        self.assertEqual(r.private_current_mmr_above_candidate_count, 0)
        self.assertEqual(r.private_current_mmr_below_candidate_count, 0)
        self.assertEqual(r.candidate_max_level_count, 3)
        self.assertTrue(r.position_bookends_consistent)
        self.assertTrue(r.public_candidate_reconstructed)
        self.assertFalse(r.future_stressed_tiers_certified)
        self.assertFalse(r.cross_wallet_collateral_certified)
        self.assertFalse(r.venue_liquidation_certified)
        self.assertFalse(r.live_authorization)
        self.assertEqual(len(private.calls), 3)
        self.assertEqual(len(public.calls), 1)
        self.assertTrue(all(call[0] == "GET" for call in private.calls + public.calls))
        self.assertNotIn("TEST_USDT", str(r))
        self.assertNotIn("2000", str(r))

    async def test_private_mmr_below_candidate_is_explicit_disagreement(self):
        x = data()
        x.private_risk_by_symbol["TEST_USDT"][0]["mmr"] = 0.001
        x.private_risk_by_symbol["TEST_USDT"][0]["currentMmr"] = 0.001
        r = await inspect_incremental_tier_candidate(
            ReadOnlyMexcProbe(FakePrivate(x)), FakePublic(x),
        )
        self.assertEqual(r.status, "INCR_PROBE_CURRENT_PRIVATE_BELOW_CANDIDATE_NONAUTHORIZING")
        self.assertEqual(r.private_current_mmr_below_candidate_count, 1)
        self.assertFalse(r.entry_allowed)

    async def test_not_incremental_never_promoted(self):
        x = frame()
        r = await inspect_incremental_tier_candidate(
            ReadOnlyMexcProbe(FakePrivate(x)), FakePublic(x),
        )
        self.assertEqual(r.status, "INCR_PROBE_CANDIDATE_UNVERIFIED")
        self.assertEqual(r.candidate_failure, "INCR_MODE_OR_TYPE_UNSUPPORTED")
        self.assertFalse(r.private_current_risk_rows_observed)

    async def test_missing_short_specific_fields_fail_closed(self):
        x = data()
        x.public_contract_by_symbol["TEST_USDT"]["riskLongShortSwitch"] = 1
        r = await inspect_incremental_tier_candidate(
            ReadOnlyMexcProbe(FakePrivate(x)), FakePublic(x),
        )
        self.assertEqual(r.candidate_failure, "INCR_SHORT_SIDE_FIELDS_MISSING")
        self.assertFalse(r.live_authorization)

    async def test_private_http_error_redacted_no_public_get(self):
        x = data()
        class FailingPrivate(FakePrivate):
            async def _private_get(self, path, params=None):
                if path == "/api/v1/private/account/risk_limit":
                    raise ValueError("PRIVATE_API_SECRET")
                return await super()._private_get(path, params)
        public = FakePublic(x)
        r = await inspect_incremental_tier_candidate(
            ReadOnlyMexcProbe(FailingPrivate(x)), public,
        )
        self.assertEqual(r.status, "INCR_PROBE_PRIVATE_RISK_GET_FAILED")
        self.assertEqual(public.calls, [])
        self.assertNotIn("PRIVATE_API_SECRET", str(r))

    async def test_public_symbol_mismatch_blocks(self):
        x = data()
        public = FakePublic(x)
        public.change_symbol = True
        r = await inspect_incremental_tier_candidate(
            ReadOnlyMexcProbe(FakePrivate(x)), public,
        )
        self.assertEqual(r.status, "INCR_PROBE_PUBLIC_CONTRACT_SCHEMA_UNVERIFIED")

    async def test_bookend_inventory_drift_blocks(self):
        x = data()
        class ChangedPrivate(FakePrivate):
            async def _private_get(self, path, params=None):
                if path == "/api/v1/private/position/open_positions" and (
                    params is None and self.positions_reads == 1
                ):
                    self.observed.positions[0]["holdVol"] = "53"
                return await super()._private_get(path, params)
        r = await inspect_incremental_tier_candidate(
            ReadOnlyMexcProbe(ChangedPrivate(x)), FakePublic(x),
        )
        self.assertEqual(r.status, "INCR_PROBE_CRITICAL_INVENTORY_DRIFT")
        self.assertFalse(r.public_candidate_reconstructed)

    async def test_paper_kill_blocks_before_credentials_and_network(self):
        reports = [
            await run(environ={}),
            await run(environ={
                "T100_EXECUTION_MODE": "paper",
                "T100_LIVE_ORDER_API_ENABLED": "false",
                "T100_LIVE_KILL_SWITCH": "true",
            }),
            await run(environ={
                "T100_EXECUTION_MODE": "live",
                "T100_LIVE_ORDER_API_ENABLED": "true",
                "T100_LIVE_KILL_SWITCH": "false",
                "MEXC_API_KEY": "SECRET_KEY",
                "MEXC_API_SECRET": "SECRET_VALUE",
            }),
        ]
        self.assertEqual(
            [r["status"] for r in reports],
            ["INCR_PROBE_PAPER_KILL_REQUIRED",
             "INCR_PROBE_CREDENTIALS_MISSING",
             "INCR_PROBE_PAPER_KILL_REQUIRED"],
        )
        self.assertTrue(all(r["entry_allowed"] is False for r in reports))
        self.assertNotIn("SECRET_KEY", str(reports))


if __name__ == "__main__":
    unittest.main()
