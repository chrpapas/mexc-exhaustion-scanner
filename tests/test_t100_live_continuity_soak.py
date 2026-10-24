"""Offline tests for bounded multi-window continuity soak."""
import os
import unittest
from unittest.mock import patch

from app.t100_live_continuity_soak import (
    ContinuitySoakConfig,
    run_continuity_soak,
)


async def no_sleep(_seconds):
    return None


class DummyClient:
    async def close(self):
        raise AssertionError("injected client should not be closed")


def stable_window(*, raw_equal=True, events=0, pongs=1):
    return {
        "status": "REST_WS_REST_CONTINUITY_STABLE",
        "critical_rest_inventory_equal": True,
        "full_raw_positions_equal": raw_equal,
        "full_raw_current_tpsl_equal": True,
        "changed_position_field_names": (
            [] if raw_equal else ["unRealizedPnl"]
        ),
        "changed_stop_field_names": [],
        "ws_observation": {
            "private_events_seen": events,
            "pongs_seen": pongs,
        },
    }


class ContinuitySoakTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.env = patch.dict(os.environ, {
            "T100_EXECUTION_MODE": "paper",
            "MEXC_API_KEY": "KEY",
            "MEXC_API_SECRET": "SECRET",
        }, clear=False)
        self.env.start()

    async def asyncTearDown(self):
        self.env.stop()

    async def test_five_stable_windows_complete_non_authorizing(self):
        calls = 0
        async def runner(**kwargs):
            nonlocal calls
            calls += 1
            return stable_window(
                raw_equal=(calls % 2 == 0),
                events=0,
                pongs=1,
            )

        r = await run_continuity_soak(
            config=ContinuitySoakConfig(
                windows=5, window_seconds=5, pause_seconds=0),
            client=DummyClient(),
            continuity_runner=runner,
            sleep=no_sleep,
        )
        self.assertEqual(
            r["status"], "COMPLETED_CONTINUITY_SOAK_NON_AUTHORIZING")
        self.assertEqual(r["completed_windows"], 5)
        self.assertEqual(r["stable_windows"], 5)
        self.assertEqual(r["full_raw_position_drift_windows"], 3)
        self.assertEqual(
            r["changed_position_field_names"], ["unRealizedPnl"])
        self.assertEqual(r["total_ws_pongs"], 5)
        self.assertFalse(r["entry_allowed"])
        self.assertFalse(r["live_authorization"])
        self.assertFalse(r["account_wide_completeness_verified"])

    async def test_critical_change_halts_immediately(self):
        statuses = [
            stable_window(),
            {
                "status": "CRITICAL_REST_INVENTORY_CHANGED_DURING_WS_WINDOW",
                "critical_rest_inventory_equal": False,
                "full_raw_positions_equal": False,
                "full_raw_current_tpsl_equal": True,
                "changed_position_field_names": ["holdVol"],
                "changed_stop_field_names": [],
                "ws_observation": {
                    "private_events_seen": 1,
                    "pongs_seen": 1,
                },
            },
            stable_window(),
        ]
        calls = 0
        async def runner(**kwargs):
            nonlocal calls
            value = statuses[calls]
            calls += 1
            return value

        r = await run_continuity_soak(
            config=ContinuitySoakConfig(
                windows=3, window_seconds=5, pause_seconds=0),
            client=DummyClient(),
            continuity_runner=runner,
            sleep=no_sleep,
        )
        self.assertEqual(r["status"], "HALTED_CONTINUITY_SOAK")
        self.assertEqual(r["completed_windows"], 2)
        self.assertEqual(r["stable_windows"], 1)
        self.assertEqual(r["critical_change_windows"], 1)
        self.assertEqual(
            r["first_failure_status"],
            "CRITICAL_REST_INVENTORY_CHANGED_DURING_WS_WINDOW",
        )
        self.assertEqual(calls, 2)

    async def test_ws_events_in_stable_status_are_counted(self):
        async def runner(**kwargs):
            return stable_window(events=2, pongs=1)

        r = await run_continuity_soak(
            config=ContinuitySoakConfig(
                windows=2, window_seconds=5, pause_seconds=0),
            client=DummyClient(),
            continuity_runner=runner,
            sleep=no_sleep,
        )
        self.assertEqual(r["ws_event_windows"], 2)
        self.assertEqual(r["total_ws_private_events"], 4)

    async def test_unexpected_ws_status_halts(self):
        async def runner(**kwargs):
            return {
                **stable_window(),
                "status": "STABLE_REST_WITH_UNEXPECTED_PRIVATE_WS_CHANNELS",
            }

        r = await run_continuity_soak(
            config=ContinuitySoakConfig(
                windows=2, window_seconds=5, pause_seconds=0),
            client=DummyClient(),
            continuity_runner=runner,
            sleep=no_sleep,
        )
        self.assertEqual(r["status"], "HALTED_CONTINUITY_SOAK")
        self.assertEqual(
            r["first_failure_status"],
            "STABLE_REST_WITH_UNEXPECTED_PRIVATE_WS_CHANNELS",
        )
        self.assertEqual(r["completed_windows"], 1)

    async def test_exception_halts_fail_closed(self):
        async def runner(**kwargs):
            raise TimeoutError()

        r = await run_continuity_soak(
            config=ContinuitySoakConfig(
                windows=2, window_seconds=5, pause_seconds=0),
            client=DummyClient(),
            continuity_runner=runner,
            sleep=no_sleep,
        )
        self.assertEqual(r["status"], "HALTED_CONTINUITY_SOAK")
        self.assertEqual(
            r["first_failure_status"], "CONTINUITY_WINDOW_EXCEPTION")
        self.assertEqual(r["failure_type"], "TimeoutError")

    async def test_nonpaper_mode_refuses_before_runner(self):
        called = False
        async def runner(**kwargs):
            nonlocal called
            called = True
            return stable_window()

        with patch.dict(
            os.environ, {"T100_EXECUTION_MODE": "live"}, clear=False
        ):
            r = await run_continuity_soak(
                config=ContinuitySoakConfig(
                    windows=2, window_seconds=5, pause_seconds=0),
                client=DummyClient(),
                continuity_runner=runner,
                sleep=no_sleep,
            )
        self.assertEqual(
            r["status"], "REFUSED_NON_PAPER_EXECUTION_MODE")
        self.assertFalse(called)

    async def test_missing_credentials_refuses(self):
        with patch.dict(os.environ, {
            "T100_EXECUTION_MODE": "paper",
            "MEXC_API_KEY": "",
            "MEXC_API_SECRET": "",
        }, clear=False):
            r = await run_continuity_soak(
                config=ContinuitySoakConfig(
                    windows=2, window_seconds=5, pause_seconds=0),
                client=DummyClient(),
                continuity_runner=lambda **kwargs: stable_window(),
                sleep=no_sleep,
            )
        self.assertEqual(
            r["status"], "MISSING_CREDENTIALS_NO_PRIVATE_READS")

    async def test_config_bounds(self):
        for windows in (0, 1, 21, True):
            with self.assertRaises(ValueError):
                ContinuitySoakConfig(windows=windows)
        for seconds in (0, 4.9, 121, True):
            with self.assertRaises(ValueError):
                ContinuitySoakConfig(window_seconds=seconds)
        for pause in (-1, 31, True):
            with self.assertRaises(ValueError):
                ContinuitySoakConfig(pause_seconds=pause)
