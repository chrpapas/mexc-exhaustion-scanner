"""Bounded REST -> private WS -> REST continuity evidence for MEXC.

Read-only only. Captures current position/TP-SL inventory before and after a
bounded authenticated private-WS observation and compares the safety-critical
projection. It does not prove account-wide completeness or atomicity.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
from dataclasses import asdict
from typing import Any, TYPE_CHECKING

if TYPE_CHECKING:
    from app.mexc_trade import MexcTradeClient

from app.t100_live_consistency import (
    CRITICAL_POSITION_FIELDS,
    CRITICAL_STOP_FIELDS,
)
from app.t100_live_guards import T100ExecutionControls
from app.t100_live_private_ws import observe_private_ws
from app.t100_live_readonly_probe import ReadOnlyMexcProbe


CONTINUITY_VERSION = "2026-10-24-rest-ws-rest-continuity-v1"


def _canonical(rows: list[dict[str, Any]], fields: tuple[str, ...]) -> tuple[str, ...]:
    return tuple(sorted(
        json.dumps(
            {key: row[key] for key in fields if key in row},
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        )
        for row in rows
    ))


def _changed_field_names(
    before: list[dict[str, Any]],
    after: list[dict[str, Any]],
) -> list[str]:
    names = {
        key
        for row in before + after
        for key in row
        if isinstance(key, str)
    }
    changed: list[str] = []
    for key in sorted(names):
        a = sorted(
            json.dumps(row.get(key), sort_keys=True, separators=(",", ":"),
                       allow_nan=False)
            for row in before
        )
        b = sorted(
            json.dumps(row.get(key), sort_keys=True, separators=(",", ":"),
                       allow_nan=False)
            for row in after
        )
        if a != b:
            changed.append(key)
    return changed


async def run_continuity_probe(
    *,
    duration_seconds: float = 30.0,
    client: MexcTradeClient | None = None,
    ws_observer=observe_private_ws,
) -> dict[str, Any]:
    result: dict[str, Any] = {
        "diagnostic_version": CONTINUITY_VERSION,
        "exchange": "MEXC",
        "read_only": True,
        "live_order_submission_possible": False,
        "entry_allowed": False,
        "live_authorization": False,
        "account_wide_completeness_verified": False,
        "exchange_atomicity_verified": False,
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
    probe = ReadOnlyMexcProbe(client)

    try:
        before_positions = await probe.raw_open_positions()
        before_stops = await probe.raw_open_stops()

        ws_report = await ws_observer(
            api_key=key,
            api_secret=secret,
            duration_seconds=duration_seconds,
        )

        after_positions = await probe.raw_open_positions()
        after_stops = await probe.raw_open_stops()

        critical_positions_equal = (
            _canonical(before_positions, CRITICAL_POSITION_FIELDS)
            == _canonical(after_positions, CRITICAL_POSITION_FIELDS)
        )
        critical_stops_equal = (
            _canonical(before_stops, CRITICAL_STOP_FIELDS)
            == _canonical(after_stops, CRITICAL_STOP_FIELDS)
        )

        full_positions_equal = (
            _canonical(
                before_positions,
                tuple(sorted({
                    key for row in before_positions + after_positions
                    for key in row if isinstance(key, str)
                })),
            )
            == _canonical(
                after_positions,
                tuple(sorted({
                    key for row in before_positions + after_positions
                    for key in row if isinstance(key, str)
                })),
            )
        )
        full_stops_equal = (
            _canonical(
                before_stops,
                tuple(sorted({
                    key for row in before_stops + after_stops
                    for key in row if isinstance(key, str)
                })),
            )
            == _canonical(
                after_stops,
                tuple(sorted({
                    key for row in before_stops + after_stops
                    for key in row if isinstance(key, str)
                })),
            )
        )

        result.update({
            "before_positions": len(before_positions),
            "after_positions": len(after_positions),
            "before_current_tpsl": len(before_stops),
            "after_current_tpsl": len(after_stops),
            "critical_positions_equal": critical_positions_equal,
            "critical_current_tpsl_equal": critical_stops_equal,
            "critical_rest_inventory_equal": (
                critical_positions_equal and critical_stops_equal
            ),
            "full_raw_positions_equal": full_positions_equal,
            "full_raw_current_tpsl_equal": full_stops_equal,
            "changed_position_field_names": _changed_field_names(
                before_positions, after_positions
            ),
            "changed_stop_field_names": _changed_field_names(
                before_stops, after_stops
            ),
            "ws_observation": asdict(ws_report),
        })

        if not critical_positions_equal or not critical_stops_equal:
            result["status"] = "CRITICAL_REST_INVENTORY_CHANGED_DURING_WS_WINDOW"
        elif ws_report.unexpected_private_channels:
            result["status"] = "STABLE_REST_WITH_UNEXPECTED_PRIVATE_WS_CHANNELS"
        elif ws_report.private_events_seen:
            result["status"] = "STABLE_REST_WITH_PRIVATE_WS_EVENTS_OBSERVED"
        else:
            result["status"] = "REST_WS_REST_CONTINUITY_STABLE"

        result["credential_status"] = (
            "SIGNED_REST_AND_PRIVATE_WS_SUCCEEDED_READ_ONLY_CONTINUITY"
        )
        return result
    except Exception as exc:
        result.update({
            "status": "HALTED_REST_WS_REST_CONTINUITY",
            "failure_type": type(exc).__name__,
        })
        return result
    finally:
        if owned:
            await client.close()


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Bounded read-only MEXC REST-WS-REST continuity probe"
    )
    parser.add_argument("--duration-seconds", type=float, default=30.0)
    args = parser.parse_args()
    result = asyncio.run(
        run_continuity_probe(duration_seconds=args.duration_seconds)
    )
    print(json.dumps(result, indent=2, sort_keys=True))
    print("NO ORDERS SENT. THIS CONTINUITY PROBE DOES NOT ENABLE LIVE TRADING.")


if __name__ == "__main__":
    main()
