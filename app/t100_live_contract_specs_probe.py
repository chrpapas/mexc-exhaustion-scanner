"""Focused GET-only MEXC public contract-detail transport diagnostic.

This tests the exact public endpoint that timed out on the Render shadow
worker, separately from the account's private REST and private WebSocket.
No API credentials, no POST, no symbol/price/leverage/contract data printed.
No live-trading permission can be inferred from its results.

Usage:
    T100_EXECUTION_MODE=paper python -m app.t100_live_contract_specs_probe
"""
from __future__ import annotations

import argparse
import asyncio
import json
import math
import os
import time
from datetime import datetime, timezone
from typing import Any, Callable, Awaitable

HOST = "https://api.mexc.com"
PATH = "/api/v1/contract/detail"
DIAGNOSTIC_VERSION = "2026-10-28-public-contract-detail-v1"


def summarize_contract_response(payload: Any) -> dict[str, Any]:
    """Allowlist-only schema assessment; never returns the payload itself."""
    if not isinstance(payload, dict):
        return {"status": "PUBLIC_CONTRACT_RESPONSE_NOT_OBJECT"}
    if payload.get("success") is not True:
        return {"status": "PUBLIC_CONTRACT_SUCCESS_NOT_TRUE"}
    data = payload.get("data")
    if not isinstance(data, list) or not data:
        return {"status": "PUBLIC_CONTRACT_DATA_NOT_NONEMPTY_LIST"}
    valid = sum(
        isinstance(row, dict)
        and isinstance(row.get("symbol"), str)
        and bool(row.get("symbol"))
        and row.get("contractSize") is not None
        for row in data
    )
    if valid == 0:
        return {
            "status": "PUBLIC_CONTRACT_NO_VALID_CONTRACT_ROWS",
            "rows_received": len(data),
            "valid_contract_rows": 0,
        }
    return {
        "status": "PUBLIC_CONTRACT_SHAPE_OBSERVED",
        "rows_received": len(data),
        "valid_contract_rows": valid,
    }


async def probe_contract_specs(
    client: Any, *,
    attempts: int = 3,
    gap_seconds: float = 2.,
    clock: Callable[[], float] = time.monotonic,
    sleep: Callable[[float], Awaitable[Any]] = asyncio.sleep,
) -> dict[str, Any]:
    if type(attempts) is not int or not 1 <= attempts <= 5:
        raise ValueError("PROBE_ATTEMPTS_OUT_OF_RANGE")
    if (
        not math.isfinite(gap_seconds) or
        not 0 <= gap_seconds <= 20
    ):
        raise ValueError("PROBE_GAP_INVALID")
    observations = []
    for index in range(attempts):
        started = clock()
        kind = "FAILED"
        data: dict[str, Any] = {}
        try:
            response = await client.get(PATH)
            response.raise_for_status()
            data = summarize_contract_response(response.json())
            kind = data["status"]
        except Exception as exc:
            # Exception classes only; never repr(exc), URLs or response bodies.
            kind = "PUBLIC_CONTRACT_GET_FAILED"
            data = {"failure_type": type(exc).__name__}
        elapsed = clock() - started
        observations.append({
            "attempt": index + 1,
            "elapsed_ms": max(0, round(elapsed * 1000)),
            "status": kind,
            **{key: value for key, value in data.items() if key != "status"},
        })
        if index + 1 < attempts:
            await sleep(gap_seconds)

    passed = sum(
        item["status"] == "PUBLIC_CONTRACT_SHAPE_OBSERVED"
        for item in observations
    )
    return {
        "diagnostic_version": DIAGNOSTIC_VERSION,
        "timestamp_utc": datetime.now(timezone.utc).isoformat(),
        "endpoint_class": "PUBLIC_CONTRACT_DETAIL",
        "read_only": True,
        "credentials_required": False,
        "mexc_orders_sent": False,
        "live_authorization": False,
        "attempts": attempts,
        "passed": passed,
        "failed": attempts - passed,
        "observations": observations,
        "status": (
            "PUBLIC_CONTRACT_DETAIL_OBSERVATION_PASSED"
            if passed == attempts
            else "PUBLIC_CONTRACT_DETAIL_OBSERVATION_INCOMPLETE"
        ),
    }


async def _main(attempts: int, gap_seconds: float) -> None:
    if os.getenv("T100_EXECUTION_MODE", "paper").strip().lower() != "paper":
        raise RuntimeError("PUBLIC_CONTRACT_PROBE_REQUIRES_PAPER_MODE")
    # No keys are read from the environment, logged or used in requests.
    import httpx
    async with httpx.AsyncClient(
        base_url=HOST,
        timeout=httpx.Timeout(20.0),
        headers={
            "User-Agent": "mexc-exhaustion-multislot-trader/1.2.0",
            "Language": "en-US",
        },
        follow_redirects=False,
    ) as client:
        result = await probe_contract_specs(
            client, attempts=attempts, gap_seconds=gap_seconds,
        )
    print(json.dumps(result, sort_keys=True, indent=2))
    print("PUBLIC GET ONLY. NO CREDENTIALS. NO ORDERS. LIVE DISABLED.")


def cli() -> None:
    parser = argparse.ArgumentParser(
        description="Read-only timing and shape of MEXC public contract detail"
    )
    parser.add_argument("--attempts", type=int, default=3)
    parser.add_argument("--gap-seconds", type=float, default=2.)
    args = parser.parse_args()
    asyncio.run(_main(args.attempts, args.gap_seconds))


if __name__ == "__main__":
    cli()
