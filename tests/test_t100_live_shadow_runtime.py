"""Offline tests for continuous Render-safe T100 live-shadow runtime."""
import os
import unittest
from unittest.mock import patch

from app.t100_live_shadow_runtime import (
    ShadowRuntimeConfig,
    T100LiveShadowRuntime,
)


class FakeNotifier:
    configured = True
    def __init__(self):
        self.calls = []
        self.closed = False
    async def send(self, notice, *, force=False):
        self.calls.append((notice, force))
        return True
    async def close(self):
        self.closed = True


def config():
    return ShadowRuntimeConfig(
        cycle_seconds=60,
        ws_window_seconds=5,
        health_report_seconds=300,
        notification_cooldown_seconds=30,
        require_discord=True,
    )


def preflight(
    *,
    positions=0,
    stops=0,
    critical=True,
    redundancy=True,
    margin=True,
    protection=None,
    protection_semantics_verified=None,
    read_failure_type=None,
):
    if protection is None:
        if positions and not stops:
            protection = "POSITION_WITHOUT_OBSERVED_CURRENT_TPSL"
        elif positions and stops:
            protection = "POSITION_AND_CURRENT_TPSL_PRESENT_LINKAGE_UNVERIFIED"
        else:
            protection = "NO_POSITION_OR_CURRENT_TPSL_OBSERVED_COMPLETENESS_UNVERIFIED"
    if protection_semantics_verified is None:
        protection_semantics_verified = bool(positions and stops)
    return {
        "credential_status": "SIGNED_PRIVATE_READS_SUCCEEDED_UNCERTIFIED_SCHEMA",
        "read_failure_type": read_failure_type,
        "critical_inventory_double_read_consistent": critical,
        "open_futures_positions": positions,
        "open_futures_stop_orders": stops,
        "t100_native_protection_presence": protection,
        "active_tpsl_semantics": {
            "active_tpsl_row_semantics_verified": protection_semantics_verified,
        },
        "symbol_scoped_inventory_crosscheck": {
            "symbol_scoped_crosscheck_verified": redundancy,
        },
        "position_inventory_economic_witness": {
            "position_inventory_margin_witness_observed": margin,
        },
    }


def continuity(status="REST_WS_REST_CONTINUITY_STABLE", events=0):
    return {
        "status": status,
        "ws_observation": {
            "private_events_seen": events,
            "position_events_seen": events,
            "stop_plan_events_seen": 0,
            "fill_events_seen": 0,
        },
    }


class ShadowRuntimeTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.env = patch.dict(os.environ, {
            "T100_EXECUTION_MODE": "paper",
            "T100_LIVE_ORDER_API_ENABLED": "false",
            "MEXC_API_KEY": "KEY",
            "MEXC_API_SECRET": "SECRET",
        }, clear=False)
        self.env.start()
        self.notifier = FakeNotifier()

    async def asyncTearDown(self):
        self.env.stop()

    def make_runtime(self, pf=None, cont=None):
        async def pf_runner(**kwargs):
            return preflight() if pf is None else pf
        async def cont_runner(**kwargs):
            return continuity() if cont is None else cont
        return T100LiveShadowRuntime(
            config=config(),
            notifier=self.notifier,
            preflight_runner=pf_runner,
            continuity_runner=cont_runner,
            clock=lambda: 1000.0,
        )

    async def test_healthy_flat_cycle_is_non_authorizing(self):
        runtime = self.make_runtime()
        report = await runtime.cycle()
        self.assertEqual(report.status, "OBSERVING_NON_AUTHORIZING")
        self.assertFalse(report.kill_tripped)
        self.assertFalse(report.entry_allowed)
        self.assertFalse(report.live_authorization)
        self.assertFalse(report.account_wide_completeness_verified)
        self.assertFalse(report.exchange_atomicity_verified)

    async def test_unprotected_position_latches_kill_once(self):
        runtime = self.make_runtime(
            pf=preflight(positions=1, stops=0))
        r1 = await runtime.cycle()
        r2 = await runtime.cycle()
        self.assertTrue(r1.kill_tripped)
        self.assertEqual(r1.kill_reason, "NATIVE_PROTECTION_LOST")
        self.assertEqual(r2.kill_reason, "NATIVE_PROTECTION_LOST")
        kills = [
            n for n, _force in self.notifier.calls
            if "KILL LATCHED" in n.title
        ]
        self.assertEqual(len(kills), 1)

    async def test_present_but_unverified_protection_latches(self):
        runtime = self.make_runtime(
            pf=preflight(
                positions=1,
                stops=1,
                protection_semantics_verified=False,
            )
        )
        report = await runtime.cycle()
        self.assertEqual(
            report.kill_reason, "NATIVE_PROTECTION_UNVERIFIED"
        )

    async def test_verified_protection_does_not_latch(self):
        runtime = self.make_runtime(
            pf=preflight(
                positions=1,
                stops=1,
                protection_semantics_verified=True,
            )
        )
        report = await runtime.cycle()
        self.assertFalse(report.kill_tripped)

    async def test_inventory_change_latches_kill(self):
        runtime = self.make_runtime(
            pf=preflight(critical=False))
        report = await runtime.cycle()
        self.assertEqual(
            report.kill_reason, "INVENTORY_CHANGED_BETWEEN_READS")

    async def test_position_margin_mismatch_latches_recovery_unverified(self):
        runtime = self.make_runtime(
            pf=preflight(positions=1, stops=1, margin=False))
        report = await runtime.cycle()
        self.assertEqual(report.kill_reason, "RECOVERY_UNVERIFIED")

    async def test_symbol_redundancy_failure_latches_recovery_unverified(self):
        runtime = self.make_runtime(
            pf=preflight(positions=1, stops=1, redundancy=False))
        report = await runtime.cycle()
        self.assertEqual(report.kill_reason, "RECOVERY_UNVERIFIED")

    async def test_continuity_critical_change_latches(self):
        runtime = self.make_runtime(
            cont=continuity(
                "CRITICAL_REST_INVENTORY_CHANGED_DURING_WS_WINDOW"))
        report = await runtime.cycle()
        self.assertEqual(
            report.kill_reason, "INVENTORY_CHANGED_BETWEEN_READS")

    async def test_continuity_halt_latches_read_failure(self):
        runtime = self.make_runtime(
            cont=continuity("HALTED_REST_WS_REST_CONTINUITY"))
        report = await runtime.cycle()
        self.assertEqual(report.kill_reason, "READ_FAILURE")

    async def test_ws_event_generates_notification(self):
        runtime = self.make_runtime(cont=continuity(events=2))
        report = await runtime.cycle()
        self.assertEqual(report.ws_private_events, 2)
        self.assertTrue(any(
            "PRIVATE ACCOUNT EVENT" in n.title
            for n, _force in self.notifier.calls
        ))

    async def test_state_change_notifies_only_on_change(self):
        runtime = self.make_runtime()
        await runtime.cycle()
        first = sum(
            "ACCOUNT STATE" in n.title
            for n, _force in self.notifier.calls
        )
        await runtime.cycle()
        second = sum(
            "ACCOUNT STATE" in n.title
            for n, _force in self.notifier.calls
        )
        self.assertEqual(first, 1)
        self.assertEqual(second, 1)

    async def test_read_failure_latches(self):
        runtime = self.make_runtime(
            pf=preflight(read_failure_type="TimeoutError"))
        report = await runtime.cycle()
        self.assertEqual(report.kill_reason, "READ_FAILURE")

    async def test_refuses_live_mode(self):
        with patch.dict(
            os.environ, {"T100_EXECUTION_MODE": "live"}, clear=False
        ):
            with self.assertRaisesRegex(RuntimeError, "REQUIRES.*PAPER"):
                self.make_runtime()

    async def test_refuses_order_api_enabled_even_in_paper(self):
        with patch.dict(
            os.environ, {"T100_LIVE_ORDER_API_ENABLED": "true"}, clear=False
        ):
            with self.assertRaisesRegex(RuntimeError, "REFUSES_ORDER_API"):
                self.make_runtime()

    async def test_requires_credentials(self):
        with patch.dict(
            os.environ, {"MEXC_API_KEY": "", "MEXC_API_SECRET": ""},
            clear=False,
        ):
            with self.assertRaisesRegex(RuntimeError, "READ_CREDENTIALS"):
                self.make_runtime()

    async def test_requires_discord_when_configured_to_require_it(self):
        class NoDiscord(FakeNotifier):
            configured = False
        with self.assertRaisesRegex(RuntimeError, "DISCORD_WEBHOOK_REQUIRED"):
            T100LiveShadowRuntime(
                config=config(),
                notifier=NoDiscord(),
                preflight_runner=lambda **kwargs: None,
                continuity_runner=lambda **kwargs: None,
            )

    async def test_runtime_config_bounds(self):
        with patch.dict(os.environ, {
            "T100_SHADOW_CYCLE_SECONDS": "10",
        }, clear=False):
            with self.assertRaises(ValueError):
                ShadowRuntimeConfig.from_env()
        with patch.dict(os.environ, {
            "T100_SHADOW_CYCLE_SECONDS": "30",
            "T100_SHADOW_WS_WINDOW_SECONDS": "30",
        }, clear=False):
            with self.assertRaises(ValueError):
                ShadowRuntimeConfig.from_env()


if __name__ == "__main__":
    unittest.main()
