"""Operator-local bounded T100 read-only MEXC soak.

GET-only. No database writes. No order/stop/position mutations.
Outputs only counts, booleans, timing and status labels.
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

from app.t100_live_guards import T100ExecutionControls
from app.t100_live_readonly_probe import ReadOnlyMexcProbe
from app.t100_live_soak import (
    DiscoverySoakConfig,
    DiscoverySoakReport,
    RawDiscoverySoakRunner,
)

SOAK_DIAGNOSTIC_VERSION = "2026-10-24-raw-discovery-soak-v1"


def _base_result() -> dict[str, Any]:
    return {
        "diagnostic_version": SOAK_DIAGNOSTIC_VERSION,
        "exchange": "MEXC",
        "read_only": True,
        "live_order_submission_possible": False,
        "raw_inventory_completeness_verified": False,
        "exchange_atomicity_verified": False,
        "entry_allowed": False,
        "live_authorization": False,
    }


async def run_read_only_soak(
    *,
    config: DiscoverySoakConfig,
    client: MexcTradeClient | None = None,
) -> dict[str, Any]:
    result = _base_result()
    controls = T100ExecutionControls.from_env()
    if controls.mode != "paper":
        result.update({
            "status": "REFUSED_NON_PAPER_EXECUTION_MODE",
            "kill_tripped": True,
            "kill_reason": "MANUAL_KILL_SWITCH",
        })
        return result

    key, secret = os.getenv("MEXC_API_KEY"), os.getenv("MEXC_API_SECRET")
    owned = client is None
    if owned and (not key or not secret):
        result.update({
            "status": "MISSING_CREDENTIALS_NO_PRIVATE_READS",
            "kill_tripped": True,
            "kill_reason": "READ_FAILURE",
        })
        return result

    if owned:
        from app.mexc_trade import MexcTradeClient
        client = MexcTradeClient(
            "https://api.mexc.com",
            api_key=key,
            api_secret=secret,
        )
    assert client is not None

    try:
        runner = RawDiscoverySoakRunner(ReadOnlyMexcProbe(client), config)
        try:
            report: DiscoverySoakReport = await asyncio.wait_for(
                runner.run(),
                timeout=float(config.max_runtime_seconds),
            )
            result.update(asdict(report))
            result["credential_status"] = (
                "SIGNED_PRIVATE_READS_SUCCEEDED_READ_ONLY_SOAK"
            )
        except asyncio.TimeoutError:
            result.update({
                "status": "HALTED_READ_ONLY_DISCOVERY",
                "kill_tripped": True,
                "kill_reason": "READ_TIMEOUT",
                "credential_status": (
                    "SIGNED_PRIVATE_READS_TIMED_OUT_READ_ONLY_SOAK"
                ),
            })
        except Exception as exc:
            result.update({
                "status": "HALTED_READ_ONLY_DISCOVERY",
                "kill_tripped": True,
                "kill_reason": "READ_FAILURE",
                "failure_type": type(exc).__name__,
                "credential_status": (
                    "SIGNED_PRIVATE_READS_FAILED_READ_ONLY_SOAK"
                ),
            })
    finally:
        if owned:
            await client.close()
    return result


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Bounded GET-only T100 MEXC discovery soak"
    )
    parser.add_argument("--samples", type=int, default=12)
    parser.add_argument("--interval-seconds", type=float, default=5.0)
    parser.add_argument("--max-runtime-seconds", type=float, default=120.0)
    parser.add_argument("--max-pair-separation-ms", type=int, default=2000)
    args = parser.parse_args()

    config = DiscoverySoakConfig(
        samples=args.samples,
        interval_seconds=args.interval_seconds,
        max_runtime_seconds=args.max_runtime_seconds,
        max_pair_separation_ms=args.max_pair_separation_ms,
    )
    result = asyncio.run(run_read_only_soak(config=config))
    print(json.dumps(result, indent=2, sort_keys=True))
    print("NO ORDERS SENT. THIS SOAK DOES NOT ENABLE LIVE TRADING.")


if __name__ == "__main__":
    main()
