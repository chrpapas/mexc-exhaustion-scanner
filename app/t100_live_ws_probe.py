"""Operator-local bounded MEXC private WebSocket observation.

Read-only/authentication-only WebSocket traffic. No order placement,
cancellation, stop modification, REST POST/DELETE or database writes.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
from dataclasses import asdict
from typing import Any

from app.t100_live_guards import T100ExecutionControls
from app.t100_live_private_ws import (
    PrivateWsHalted,
    observe_private_ws,
)


WS_PROBE_VERSION = "2026-10-24-private-ws-observer-v1"


async def run_ws_probe(*, duration_seconds: float = 30.0) -> dict[str, Any]:
    result: dict[str, Any] = {
        "diagnostic_version": WS_PROBE_VERSION,
        "exchange": "MEXC",
        "read_only": True,
        "live_order_submission_possible": False,
        "entry_allowed": False,
        "live_authorization": False,
        "rest_snapshot_completeness_verified": False,
        "exchange_atomicity_verified": False,
    }

    controls = T100ExecutionControls.from_env()
    if controls.mode != "paper":
        result.update({
            "status": "REFUSED_NON_PAPER_EXECUTION_MODE",
            "login_verified": False,
        })
        return result

    key = os.getenv("MEXC_API_KEY") or ""
    secret = os.getenv("MEXC_API_SECRET") or ""
    if not key or not secret:
        result.update({
            "status": "MISSING_CREDENTIALS_NO_WS_LOGIN",
            "login_verified": False,
        })
        return result

    try:
        report = await observe_private_ws(
            api_key=key,
            api_secret=secret,
            duration_seconds=duration_seconds,
        )
        result.update(asdict(report))
        result["credential_status"] = (
            "PRIVATE_WS_LOGIN_SUCCEEDED_READ_ONLY_OBSERVATION"
        )
    except PrivateWsHalted as exc:
        result.update({
            "status": "HALTED_PRIVATE_WS_OBSERVATION",
            "login_verified": False,
            "failure_type": type(exc).__name__,
        })
    except Exception as exc:
        result.update({
            "status": "HALTED_PRIVATE_WS_OBSERVATION",
            "login_verified": False,
            "failure_type": type(exc).__name__,
        })
    return result


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Bounded read-only MEXC Futures private WS observer"
    )
    parser.add_argument("--duration-seconds", type=float, default=30.0)
    args = parser.parse_args()
    result = asyncio.run(
        run_ws_probe(duration_seconds=args.duration_seconds)
    )
    print(json.dumps(result, indent=2, sort_keys=True))
    print("NO ORDERS SENT. THIS WS OBSERVER DOES NOT ENABLE LIVE TRADING.")


if __name__ == "__main__":
    main()
