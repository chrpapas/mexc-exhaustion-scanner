"""Offline opt-in redacted risk diagnostic tests; no credentials or HTTP."""
from __future__ import annotations

import asyncio
import unittest

from app.t100_live_cross_risk_diagnostic import (
    CrossRiskDiagnosticHalted, _main, _result,
    require_readonly_controls, run_diagnostic,
)
from app.t100_live_readonly_probe import ReadOnlyMexcProbe
from tests.test_t100_live_cross_risk_collector import (
    FakePrivate, FakePublic,
)
from tests.test_t100_live_cross_risk_observation import frame


def safe_env(**extras):
    env = {
        "T100_EXECUTION_MODE": "paper",
        "T100_LIVE_ORDER_API_ENABLED": "false",
        "T100_LIVE_KILL_SWITCH": "true",
    }
    env.update(extras)
    return env


class SequenceClock:
    def __init__(self, values):
        self.values = iter(values)

    def __call__(self):
        return next(self.values)


class DiagnosticTests(unittest.IsolatedAsyncioTestCase):
    def test_accepts_only_explicit_paper_disabled_and_killed(self):
        require_readonly_controls(safe_env())
        with self.assertRaisesRegex(CrossRiskDiagnosticHalted, "PAPER_KILL_REQUIRED"):
            require_readonly_controls({})
        for changes in (
            {"T100_EXECUTION_MODE": "live"},
            {"T100_LIVE_ORDER_API_ENABLED": "true"},
            {"T100_LIVE_KILL_SWITCH": "false"},
        ):
            with self.subTest(changes=changes):
                with self.assertRaisesRegex(
                    CrossRiskDiagnosticHalted, "PAPER_KILL_REQUIRED",
                ):
                    require_readonly_controls(safe_env(**changes))

    def test_invalid_control_config_halts(self):
        with self.assertRaisesRegex(CrossRiskDiagnosticHalted, "CONTROLS_INVALID"):
            require_readonly_controls(safe_env(T100_LIVE_LEVERAGE="no"))

    async def test_missing_credentials_never_imports_network_client(self):
        result = await _main(environ=safe_env())
        self.assertEqual(result["status"], "RISK_DIAGNOSTIC_CREDENTIALS_MISSING")
        self.assertFalse(result["live_authorization"])

    async def test_live_setting_is_rejected_before_credential_use(self):
        result = await _main(environ=safe_env(
            T100_EXECUTION_MODE="live",
            MEXC_API_KEY="SHOULD_NOT_USE",
            MEXC_API_SECRET="SHOULD_NOT_USE",
        ))
        self.assertEqual(result["status"], "RISK_DIAGNOSTIC_PAPER_KILL_REQUIRED")
        self.assertNotIn("SHOULD_NOT_USE", str(result))

    async def test_two_valid_read_only_frames_emit_only_redacted_counts(self):
        underlying = frame()
        private = FakePrivate(underlying)
        public = FakePublic(underlying, fair_ms=1_000_400)
        class MovingPublic:
            async def get(inner, path, *, params=None):
                if len(public.calls) >= 2:
                    public.fair_ms = 1_001_400
                return await public.get(path, params=params)

        clock = SequenceClock([
            1_000_000, 1_000_500, 1_000_700,
            1_001_000, 1_001_500, 1_001_700,
        ])
        report = await run_diagnostic(
            private=ReadOnlyMexcProbe(private),
            public=MovingPublic(),
            clock_ms=clock,
        )
        self.assertEqual(report["status"], "CURRENT_CROSS_RISK_OBSERVED_NONAUTHORIZING")
        self.assertEqual(report["observed_positions"], 1)
        self.assertTrue(report["critical_inventory_stable"])
        self.assertTrue(report["position_margin_economic_witness_observed"])
        self.assertTrue(all(x[0] == "GET" for x in private.calls + public.calls))
        self.assertFalse(report["venue_liquidation_certified"])
        self.assertFalse(report["t100_position_ownership_certified"])
        self.assertFalse(report["cross_wallet_collateral_certified"])
        self.assertFalse(report["entry_allowed"])
        self.assertFalse(report["live_authorization"])
        self.assertNotIn("TEST_USDT", str(report))
        self.assertNotIn("2000", str(report))
        self.assertNotIn("1_000_000", str(report))

    async def test_private_timeout_is_redacted_and_closed(self):
        underlying = frame()
        private = FakePrivate(underlying)
        private.fail = True
        report = await run_diagnostic(
            private=ReadOnlyMexcProbe(private),
            public=FakePublic(underlying),
            clock_ms=lambda: 1_000_000,
        )
        self.assertEqual(report["status"], "CROSS_RISK_READ_OR_EVIDENCE_UNVERIFIED")
        self.assertNotIn("DO_NOT_LEAK", str(report))

    async def test_failure_reports_fixed_read_stage_and_unknown_position_count(self):
        base = frame()
        fake = FakePrivate(base)
        fake.fail = True
        report = await run_diagnostic(
            private=ReadOnlyMexcProbe(fake),
            public=FakePublic(base),
            clock_ms=lambda: 1_000_000,
        )
        self.assertEqual(report["status"], "CROSS_RISK_READ_OR_EVIDENCE_UNVERIFIED")
        self.assertEqual(report["failure_stage"], "READ_FIRST")
        self.assertEqual(report["failure_code"], "COLLECTOR_READ_FAILED_AT_PRIVATE_POSITIONS")
        self.assertIsNone(report["observed_positions"])
        self.assertNotIn("DO_NOT_LEAK_PRIVATE_KEY", str(report))
        self.assertFalse(report["entry_allowed"])

    async def test_flat_account_is_classified_without_faking_risk_certification(self):
        base = frame()
        base.positions.clear()
        base.stops.clear()
        private = FakePrivate(base)
        public = FakePublic(base)
        report = await run_diagnostic(
            private=ReadOnlyMexcProbe(private), public=public,
            clock_ms=lambda: 1_000_000,
        )
        self.assertEqual(
            report["failure_code"], "COLLECTOR_NONFLAT_ONE_TO_FOUR_REQUIRED"
        )
        self.assertEqual(report["failure_stage"], "READ_FIRST")
        self.assertEqual(
            report["inventory_shape_status"],
            "INVENTORY_SHAPE_FLAT_AND_ORDER_FREE_OBSERVED_NONATOMIC",
        )
        self.assertEqual(report["observed_positions"], 0)
        self.assertEqual(report["observed_current_stops"], 0)
        self.assertEqual(report["reported_total_open_orders"], 0)
        self.assertTrue(report["inventory_shape_stable"])
        self.assertFalse(report["account_wide_completeness_certified"])
        self.assertFalse(report["venue_liquidation_certified"])
        self.assertFalse(report["live_authorization"])
        self.assertEqual(public.calls, [])

    async def test_mismatched_stop_count_is_diagnosed_without_trading(self):
        base = frame()
        base.stops.clear()
        private = FakePrivate(base)
        report = await run_diagnostic(
            private=ReadOnlyMexcProbe(private), public=FakePublic(base),
            clock_ms=lambda: 1_000_000,
        )
        self.assertEqual(
            report["inventory_shape_status"],
            "INVENTORY_SHAPE_NONFLAT_STOP_ROW_COUNT_DIFFERS",
        )
        self.assertEqual(report["observed_positions"], 1)
        self.assertEqual(report["observed_current_stops"], 0)
        self.assertFalse(report["entry_allowed"])

    async def test_timeout_does_not_raise_or_enable_entry(self):
        class Slow:
            async def raw_open_positions(self):
                await asyncio.sleep(1.)
        # Pass a deliberately wrong reader; collector rejects it and
        # diagnostic must not allow a trusted result.
        report = await run_diagnostic(
            private=Slow(), public=FakePublic(frame()),
            clock_ms=lambda: 1_000_000,
            timeout_seconds=0.02,
        )
        self.assertNotEqual(
            report["status"], "CURRENT_CROSS_RISK_OBSERVED_NONAUTHORIZING",
        )
        self.assertFalse(report["live_authorization"])

    def test_only_fixed_allowlisted_statuses_survive(self):
        report = _result(
            status="http://SECRET_HOST?apikey=SECRET",
            positions=900,
        )
        self.assertEqual(report["status"], "RISK_DIAGNOSTIC_GET_FAILED")
        self.assertIsNone(report["observed_positions"])
        self.assertNotIn("SECRET", str(report))
        self.assertNotIn("TEST_USDT", str(report))
        self.assertFalse(report["entry_allowed"])


if __name__ == "__main__":
    unittest.main()
