"""Offline GET-only public risk schedule probe tests: zero orders."""
from __future__ import annotations

import copy
import unittest

from app.t100_live_readonly_probe import ReadOnlyMexcProbe
from app.t100_live_risk_schedule_probe import inspect_risk_schedule_shape
from app.t100_live_risk_schedule_probe_cli import run
from tests.test_t100_live_cross_risk_collector import FakePrivate, FakePublic
from tests.test_t100_live_cross_risk_observation import frame


class ProbeTests(unittest.IsolatedAsyncioTestCase):
    async def _check(self, data):
        private = FakePrivate(data)
        public = FakePublic(data)
        r = await inspect_risk_schedule_shape(
            ReadOnlyMexcProbe(private), public,
        )
        self.assertTrue(all(item[0] == "GET" for item in private.calls + public.calls))
        self.assertEqual(
            sum(c[1] == "/api/v1/private/position/open_positions" for c in private.calls),
            2,
        )
        self.assertEqual(len(public.calls), 1)
        self.assertFalse(r.entry_allowed)
        self.assertFalse(r.live_authorization)
        self.assertFalse(r.current_private_risk_tiers_certified)
        self.assertFalse(r.future_stressed_tiers_certified)
        self.assertFalse(r.cross_wallet_collateral_certified)
        self.assertFalse(r.risk_schedule_two_frame_certified)
        self.assertFalse(r.exchange_atomicity_verified)
        self.assertNotIn("TEST_USDT", str(r))
        self.assertNotIn("2000", str(r))
        return r

    async def test_custom_volume(self):
        r = await self._check(frame())
        self.assertEqual(r.risk_schedule_mode, "CUSTOM")
        self.assertEqual(r.risk_schedule_type, "BY_VOLUME")
        self.assertEqual(r.risk_schedule_fields, "CUSTOM_FIELDS_PRESENT")
        self.assertEqual(r.observed_positions, 1)
        self.assertTrue(r.position_bookends_consistent)

    async def test_incremental_value_surfaces_without_pnl_gets(self):
        x = frame()
        x.public_contract_by_symbol["TEST_USDT"].update({
            "riskLimitMode": "INCREASE", "riskLimitType": "BY_VALUE",
            "riskBaseVol": 10000, "riskIncrVol": 2000,
            "riskIncrMmr": 0.005, "riskIncrImr": 0.005,
            "maintenanceMarginRate": 0.01, "initialMarginRate": 0.02,
            "riskLevelLimit": 5,
        })
        r = await self._check(x)
        self.assertEqual(r.status, "RISK_SHAPE_PUBLIC_METADATA_OBSERVED_NONAUTHORIZING")
        self.assertEqual(r.risk_schedule_mode, "INCREASE")
        self.assertEqual(r.risk_schedule_type, "BY_VALUE")
        self.assertEqual(r.risk_schedule_fields, "INCREMENTAL_FIELDS_PRESENT_UNMODELED")

    async def test_malformed_public_payload_fails_without_values(self):
        x = frame()
        p = FakePrivate(x)
        pub = FakePublic(x)
        pub.change_symbol = True
        r = await inspect_risk_schedule_shape(ReadOnlyMexcProbe(p), pub)
        self.assertEqual(r.status, "RISK_SHAPE_PUBLIC_SCHEMA_UNVERIFIED")
        self.assertIsNone(r.observed_positions)
        self.assertFalse(r.entry_allowed)

    async def test_snapshot_position_drift_fails_and_does_not_report_shape(self):
        x = frame()
        class ChangingPrivate(FakePrivate):
            async def _private_get(self, path, params=None):
                if path == "/api/v1/private/position/open_positions" and (
                    params is None and self.positions_reads == 1
                ):
                    self.observed.positions[0]["holdVol"] = "123"
                return await super()._private_get(path, params)
        r = await inspect_risk_schedule_shape(
            ReadOnlyMexcProbe(ChangingPrivate(x)), FakePublic(x),
        )
        self.assertEqual(r.status, "RISK_SHAPE_POSITION_BOOKEND_DRIFT")
        self.assertEqual(r.risk_schedule_mode, "NOT_CHECKED")
        self.assertFalse(r.live_authorization)

    async def test_failed_private_get_does_not_leak_raw_exception(self):
        x = frame()
        private = FakePrivate(x)
        private.fail = True
        r = await inspect_risk_schedule_shape(ReadOnlyMexcProbe(private), FakePublic(x))
        self.assertEqual(r.status, "RISK_SHAPE_PRIVATE_GET_FAILED")
        self.assertNotIn("DO_NOT_LEAK", str(r))

    async def test_zero_positions_no_public_get_and_non_authorizing(self):
        x = frame()
        x.positions.clear()
        public = FakePublic(x)
        r = await inspect_risk_schedule_shape(
            ReadOnlyMexcProbe(FakePrivate(x)), public,
        )
        self.assertEqual(r.status, "RISK_SHAPE_INVENTORY_UNVERIFIED")
        self.assertEqual(public.calls, [])
        self.assertFalse(r.entry_allowed)

    async def test_preflight_blocks_without_client_import(self):
        for env, expected in (
            ({}, "RISK_SHAPE_PAPER_KILL_REQUIRED"),
            ({
                "T100_EXECUTION_MODE": "live",
                "T100_LIVE_ORDER_API_ENABLED": "false",
                "T100_LIVE_KILL_SWITCH": "true",
                "MEXC_API_KEY": "DUMMY_KEY",
                "MEXC_API_SECRET": "DUMMY_SECRET",
            }, "RISK_SHAPE_PAPER_KILL_REQUIRED"),
            ({
                "T100_EXECUTION_MODE": "paper",
                "T100_LIVE_ORDER_API_ENABLED": "false",
                "T100_LIVE_KILL_SWITCH": "true",
            }, "RISK_SHAPE_CREDENTIALS_MISSING"),
        ):
            report = await run(environ=env)
            self.assertEqual(report["status"], expected)
            self.assertFalse(report["mexc_orders_sent"])
            self.assertNotIn("DUMMY", str(report))


if __name__ == "__main__":
    unittest.main()
