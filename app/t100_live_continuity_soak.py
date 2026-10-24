"""Bounded multi-window REST-WS-REST continuity soak.

Read-only and non-authorizing. Repeats the continuity sandwich a bounded
number of times and fails closed on the first critical/transport/WS anomaly.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
from dataclasses import dataclass
from typing import Any, Awaitable, Callable, TYPE_CHECKING

if TYPE_CHECKING:
    from app.mexc_trade import MexcTradeClient

from app.t100_live_continuity_probe import run_continuity_probe
from app.t100_live_guards import T100ExecutionControls


CONTINUITY_SOAK_VERSION = "2026-10-24-rest-ws-rest-soak-v1"
PASS_STATUSES = frozenset({"REST_WS_REST_CONTINUITY_STABLE"})


@dataclass(frozen=True)
class ContinuitySoakConfig:
    windows: int = 5
    window_seconds: float = 30.0
    pause_seconds: float = 2.0

    def __post_init__(self) -> None:
        if type(self.windows) is not int or not 2 <= self.windows <= 20:
            raise ValueError("INVALID_CONTINUITY_SOAK_WINDOWS")
        if (
            isinstance(self.window_seconds, bool)
            or not isinstance(self.window_seconds, (int, float))
            or not 5 <= float(self.window_seconds) <= 120
        ):
            raise ValueError("INVALID_CONTINUITY_WINDOW_SECONDS")
        if (
            isinstance(self.pause_seconds, bool)
            or not isinstance(self.pause_seconds, (int, float))
            or not 0 <= float(self.pause_seconds) <= 30
        ):
            raise ValueError("INVALID_CONTINUITY_PAUSE_SECONDS")


async def run_continuity_soak(
    *,
    config: ContinuitySoakConfig,
    client: MexcTradeClient | None = None,
    continuity_runner: Callable[..., Awaitable[dict[str, Any]]] = run_continuity_probe,
    sleep: Callable[[float], Awaitable[Any]] = asyncio.sleep,
) -> dict[str, Any]:
    result: dict[str, Any] = {
        "diagnostic_version": CONTINUITY_SOAK_VERSION,
        "exchange": "MEXC",
        "read_only": True,
        "live_order_submission_possible": False,
        "entry_allowed": False,
        "live_authorization": False,
        "account_wide_completeness_verified": False,
        "exchange_atomicity_verified": False,
        "requested_windows": config.windows,
        "completed_windows": 0,
        "stable_windows": 0,
        "critical_change_windows": 0,
        "ws_event_windows": 0,
        "full_raw_position_drift_windows": 0,
        "full_raw_stop_drift_windows": 0,
        "changed_position_field_names": [],
        "changed_stop_field_names": [],
        "total_ws_private_events": 0,
        "total_ws_pongs": 0,
        "first_failure_status": None,
    }

    controls = T100ExecutionControls.from_env()
    if controls.mode != "paper":
        result["status"] = "REFUSED_NON_PAPER_EXECUTION_MODE"
        return result

    key = os.getenv("MEXC_API_KEY") or ""
    secret = os.getenv("MEXC_API_SECRET") or ""
    if not key or not secret:
        result["status"] = "MISSING_CREDENTIALS_NO_PRIVATE_READS"
        return result

    owned = client is None
    if owned:
        from app.mexc_trade import MexcTradeClient
        client = MexcTradeClient(
            "https://api.mexc.com",
            api_key=key,
            api_secret=secret,
        )
    assert client is not None

    changed_position_fields: set[str] = set()
    changed_stop_fields: set[str] = set()

    try:
        for index in range(config.windows):
            window = await continuity_runner(
                duration_seconds=float(config.window_seconds),
                client=client,
            )
            result["completed_windows"] += 1

            for field in window.get("changed_position_field_names", []):
                if isinstance(field, str):
                    changed_position_fields.add(field)
            for field in window.get("changed_stop_field_names", []):
                if isinstance(field, str):
                    changed_stop_fields.add(field)

            if window.get("full_raw_positions_equal") is False:
                result["full_raw_position_drift_windows"] += 1
            if window.get("full_raw_current_tpsl_equal") is False:
                result["full_raw_stop_drift_windows"] += 1

            ws = window.get("ws_observation")
            if isinstance(ws, dict):
                events = ws.get("private_events_seen")
                pongs = ws.get("pongs_seen")
                if type(events) is int and events >= 0:
                    result["total_ws_private_events"] += events
                    if events > 0:
                        result["ws_event_windows"] += 1
                if type(pongs) is int and pongs >= 0:
                    result["total_ws_pongs"] += pongs

            status = window.get("status")
            if status in PASS_STATUSES:
                result["stable_windows"] += 1
            else:
                if status == "CRITICAL_REST_INVENTORY_CHANGED_DURING_WS_WINDOW":
                    result["critical_change_windows"] += 1
                result["first_failure_status"] = (
                    status if isinstance(status, str) else "UNKNOWN_WINDOW_FAILURE"
                )
                result["status"] = "HALTED_CONTINUITY_SOAK"
                break

            if index + 1 < config.windows:
                await sleep(float(config.pause_seconds))

        result["changed_position_field_names"] = sorted(
            changed_position_fields
        )
        result["changed_stop_field_names"] = sorted(changed_stop_fields)

        if (
            result["completed_windows"] == config.windows
            and result["stable_windows"] == config.windows
            and result["first_failure_status"] is None
        ):
            result["status"] = "COMPLETED_CONTINUITY_SOAK_NON_AUTHORIZING"

        result["credential_status"] = (
            "SIGNED_REST_AND_PRIVATE_WS_SUCCEEDED_READ_ONLY_CONTINUITY_SOAK"
        )
        return result
    except Exception as exc:
        result["changed_position_field_names"] = sorted(
            changed_position_fields
        )
        result["changed_stop_field_names"] = sorted(changed_stop_fields)
        result.update({
            "status": "HALTED_CONTINUITY_SOAK",
            "first_failure_status": "CONTINUITY_WINDOW_EXCEPTION",
            "failure_type": type(exc).__name__,
        })
        return result
    finally:
        if owned:
            await client.close()


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Bounded read-only MEXC REST-WS-REST continuity soak"
    )
    parser.add_argument("--windows", type=int, default=5)
    parser.add_argument("--window-seconds", type=float, default=30.0)
    parser.add_argument("--pause-seconds", type=float, default=2.0)
    args = parser.parse_args()
    result = asyncio.run(run_continuity_soak(
        config=ContinuitySoakConfig(
            windows=args.windows,
            window_seconds=args.window_seconds,
            pause_seconds=args.pause_seconds,
        )
    ))
    print(json.dumps(result, indent=2, sort_keys=True))
    print("NO ORDERS SENT. THIS CONTINUITY SOAK DOES NOT ENABLE LIVE TRADING.")


if __name__ == "__main__":
    main()
