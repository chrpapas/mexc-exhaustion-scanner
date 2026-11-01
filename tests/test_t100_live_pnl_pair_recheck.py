"""Non-authorizing GET-only redacted PNL discrepancy follow-up tests."""
from __future__ import annotations

import copy
import unittest

from app.t100_live_cross_risk_diagnostic import run_diagnostic
from app.t100_live_pnl_pair_recheck import recheck_pnl_pair_readonly
from app.t100_live_readonly_probe import ReadOnlyMexcProbe
from tests.test_t100_live_cross_risk_collector import FakePrivate, FakePublic
from tests.test_t100_live_cross_risk_observation import frame


class PnlRecheckTests(unittest.IsolatedAsyncioTestCase):
    async def test_stable_two_pairs_match_without_certifying_risk(self):
        private = FakePrivate(frame())
        out = await recheck_pnl_pair_readonly(ReadOnlyMexcProbe(private))
        self.assertEqual(
            out.status, "PNL_RECHECK_TWO_PAIRS_MATCH_NONAUTHORIZING"
        )
        self.assertEqual(out.pairs_observed, 2)
        self.assertTrue(out.positions_stable_across_pairs)
        self.assertTrue(out.first_pair_reconciled)
        self.assertTrue(out.second_pair_reconciled)
        self.assertFalse(out.risk_certified)
        self.assertFalse(out.entry_allowed)
        self.assertFalse(out.live_authorization)
        self.assertFalse(out.mexc_orders_sent)
        self.assertTrue(all(method == "GET" for method, *_ in private.calls))
        self.assertEqual(len(private.calls), 4)

    async def test_persistent_usdt_aggregate_disagreement_stays_blocked(self):
        data = frame()
        data.assets[0]["unrealized"] = "987.54321"
        private = FakePrivate(data)
        out = await recheck_pnl_pair_readonly(ReadOnlyMexcProbe(private))
        self.assertEqual(
            out.status, "PNL_RECHECK_PERSISTENT_PNL_MISMATCH_NONAUTHORIZING",
        )
        self.assertFalse(out.first_pair_reconciled)
        self.assertFalse(out.second_pair_reconciled)
        self.assertFalse(out.position_unrealized_moved_across_pairs)
        self.assertFalse(out.asset_unrealized_moved_across_pairs)
        self.assertNotIn("987.54321", str(out))
        self.assertFalse(out.live_authorization)

    async def test_mark_to_market_changes_during_reads_are_observed_not_accepted(self):
        class MovingPnl(FakePrivate):
            async def _private_get(self, path, params=None):
                if (
                    path == "/api/v1/private/position/open_positions"
                    and params is None and self.positions_reads == 1
                ):
                    self.observed.positions[0]["unRealizedPnl"] = "3.5"
                    self.observed.assets[0]["unrealized"] = "3.5"
                return await super()._private_get(path, params)

        private = MovingPnl(frame())
        out = await recheck_pnl_pair_readonly(ReadOnlyMexcProbe(private))
        self.assertEqual(
            out.status, "PNL_RECHECK_MARK_TO_MARKET_MOVED_NONAUTHORIZING",
        )
        self.assertTrue(out.position_unrealized_moved_across_pairs)
        self.assertTrue(out.asset_unrealized_moved_across_pairs)
        self.assertTrue(out.first_pair_reconciled)
        self.assertTrue(out.second_pair_reconciled)
        self.assertFalse(out.risk_certified)

    async def test_margin_or_position_identity_drift_never_passes(self):
        class MarginDrift(FakePrivate):
            async def _private_get(self, path, params=None):
                if (
                    path == "/api/v1/private/position/open_positions"
                    and params is None and self.positions_reads == 1
                ):
                    self.observed.positions[0]["im"] = "99999"
                return await super()._private_get(path, params)

        out = await recheck_pnl_pair_readonly(
            ReadOnlyMexcProbe(MarginDrift(frame())),
        )
        self.assertEqual(
            out.status, "PNL_RECHECK_POSITION_IDENTITY_OR_MARGIN_DRIFT",
        )
        self.assertFalse(out.positions_stable_across_pairs)
        self.assertFalse(out.risk_certified)

    async def test_missing_pnl_or_api_failure_fails_closed_without_leak(self):
        data = frame()
        data.positions[0].pop("unRealizedPnl")
        out = await recheck_pnl_pair_readonly(
            ReadOnlyMexcProbe(FakePrivate(data)),
        )
        self.assertEqual(
            out.status, "PNL_RECHECK_READ_OR_SCHEMA_UNVERIFIED",
        )
        broken = FakePrivate(frame())
        broken.fail = True
        out = await recheck_pnl_pair_readonly(ReadOnlyMexcProbe(broken))
        self.assertEqual(
            out.status, "PNL_RECHECK_READ_OR_SCHEMA_UNVERIFIED",
        )
        self.assertNotIn("DO_NOT_LEAK", str(out))
        self.assertFalse(out.entry_allowed)

    async def test_original_post_pnl_failure_triggers_recheck_but_not_certification(self):
        # The POST asset GET alone disagrees: this models a torn read.
        # The recheck observes two correct, stable subsequent pairs.
        class PostAssetOneOffMismatch(FakePrivate):
            def __init__(self, observed):
                super().__init__(observed)
                self.assets_reads = 0

            async def _private_get(self, path, params=None):
                result = await super()._private_get(path, params)
                if path == "/api/v1/private/account/assets":
                    self.assets_reads += 1
                    if self.assets_reads == 2:
                        result[0]["unrealized"] = "99999"
                return result

        private = PostAssetOneOffMismatch(frame())
        report = await run_diagnostic(
            private=ReadOnlyMexcProbe(private),
            public=FakePublic(private.observed, fair_ms=1_000_400),
            clock_ms=iter([1_000_000, 1_000_500]).__next__,
        )
        self.assertEqual(
            report["failure_code"], "COLLECTOR_POST_UNREALIZED_PNL_MISMATCH",
        )
        self.assertEqual(report["failure_stage"], "READ_FIRST")
        self.assertEqual(
            report["pnl_pair_recheck_status"],
            "PNL_RECHECK_TWO_PAIRS_MATCH_NONAUTHORIZING",
        )
        self.assertEqual(report["pnl_pair_recheck_pairs_observed"], 2)
        self.assertFalse(report["account_wide_completeness_certified"])
        self.assertFalse(report["cross_wallet_collateral_certified"])
        self.assertFalse(report["entry_allowed"])
        self.assertFalse(report["live_authorization"])
        self.assertFalse(report["mexc_orders_sent"])
        self.assertNotIn("99999", str(report))
        self.assertTrue(all(method == "GET" for method, *_ in private.calls))

    async def test_transient_pnl_mismatch_then_whole_fresh_window_passes(self):
        # First observation fails on a single torn POST asset GET.
        # Two new PNL pairs match; BOTH subsequent complete risk frames
        # must satisfy all ordinary collector and tier checks.
        class BadOneAssetRead(FakePrivate):
            def __init__(self, data):
                super().__init__(data)
                self.asset_reads = 0

            async def _private_get(self, path, params=None):
                rows = await super()._private_get(path, params)
                if path == "/api/v1/private/account/assets":
                    self.asset_reads += 1
                    if self.asset_reads == 2:
                        rows[0]["unrealized"] = "99999"
                return rows

        private = BadOneAssetRead(frame())
        ts = iter([
            1_000_000, 1_000_500,
            1_001_000, 1_001_500, 1_001_700,
            1_002_000, 1_002_500, 1_002_700,
        ])
        result = await run_diagnostic(
            private=ReadOnlyMexcProbe(private),
            public=FakePublic(private.observed, fair_ms=1_000_400),
            clock_ms=ts.__next__,
        )
        self.assertEqual(
            result["initial_pnl_window_failure_code"],
            "COLLECTOR_POST_UNREALIZED_PNL_MISMATCH",
        )
        self.assertEqual(
            result["pnl_pair_recheck_status"],
            "PNL_RECHECK_TWO_PAIRS_MATCH_NONAUTHORIZING",
        )
        self.assertEqual(
            result["pnl_full_window_retry_status"],
            "CURRENT_CROSS_RISK_OBSERVED_NONAUTHORIZING",
        )
        self.assertEqual(
            result["status"], "CURRENT_CROSS_RISK_OBSERVED_NONAUTHORIZING",
        )
        self.assertTrue(result["critical_inventory_stable"])
        self.assertTrue(result["position_margin_economic_witness_observed"])
        self.assertFalse(result["cross_wallet_collateral_certified"])
        self.assertFalse(result["venue_liquidation_certified"])
        self.assertFalse(result["entry_allowed"])
        self.assertFalse(result["live_authorization"])
        self.assertFalse(result["mexc_orders_sent"])
        self.assertTrue(all(method == "GET" for method, *_ in private.calls))
        self.assertNotIn("99999", str(result))

    async def test_stable_persistent_mismatch_diagnostic_is_distinguished(self):
        data = frame()
        data.assets[0]["unrealized"] = "13.775"
        private = FakePrivate(data)
        report = await run_diagnostic(
            private=ReadOnlyMexcProbe(private),
            public=FakePublic(data, fair_ms=1_000_400),
            clock_ms=iter([1_000_000, 1_000_500]).__next__,
        )
        self.assertEqual(
            report["failure_code"], "COLLECTOR_PRE_UNREALIZED_PNL_MISMATCH",
        )
        self.assertEqual(
            report["pnl_pair_recheck_status"],
            "PNL_RECHECK_PERSISTENT_PNL_MISMATCH_NONAUTHORIZING",
        )
        self.assertFalse(report["live_authorization"])


if __name__ == "__main__":
    unittest.main()
