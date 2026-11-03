"""Manual-only, redacted CURRENT-account MEXC completeness check.

Runs independently of the deployed Render shadow. Uses read-only private
GET endpoints only, never publishes symbols, IDs, balances, price, quantity,
response bodies or credentials. Non-atomic, non-authorizing evidence.

From trusted Mac with pre-existing read credentials:
    T100_EXECUTION_MODE=paper python -m app.t100_live_current_inventory_cli
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
from typing import Any

from app.t100_live_guards import T100ExecutionControls
from app.t100_live_inventory_completeness_probe import (
    run_current_inventory_completeness_probe,
)
from app.t100_live_readonly_probe import ReadOnlyMexcProbe


def shareable_current_inventory_report(report: Any) -> dict[str, Any]:
    """Strict allowlist output: no raw venue/financial/identity payloads."""
    payload = report if isinstance(report, dict) else {}
    witness = payload.get("position_inventory_economic_witness")
    if not isinstance(witness, dict):
        witness = {}
    crosscheck = payload.get("symbol_scoped_inventory_crosscheck")
    if not isinstance(crosscheck, dict):
        crosscheck = {}
    certificate = payload.get("current_inventory_completeness")
    if not isinstance(certificate, dict):
        certificate = {}
    counts = payload.get("open_order_counts")
    if not isinstance(counts, dict):
        counts = {}

    allowed_counts = {}
    for key in (
        "sumCount", "limitOrderCount", "stopOrderCount",
        "planOrderCount", "trackOrderCount",
    ):
        value = counts.get(key)
        allowed_counts[key] = value if type(value) is int and value >= 0 else None

    result = {
        "status": str(payload.get("status") or "CURRENT_INVENTORY_PROBE_MISSING"),
        "failure_type": str(payload.get("failure_type") or "none")[:64],
        "read_only": True,
        "mexc_private_get_only": True,
        "mexc_orders_sent": False,
        "entry_allowed": False,
        "live_authorization": False,
        "account_wide_completeness_verified": (
            payload.get("account_wide_completeness_verified") is True
        ),
        "exchange_atomicity_verified": False,
        "before_positions": payload.get("before_positions"),
        "after_positions": payload.get("after_positions"),
        "before_current_tpsl": payload.get("before_current_tpsl"),
        "after_current_tpsl": payload.get("after_current_tpsl"),
        "critical_inventory_equal_across_witness_window": (
            payload.get("critical_inventory_equal_across_witness_window") is True
        ),
        "current_tpsl_count_matches": (
            certificate.get("current_tpsl_count_matches") is True
        ),
        "position_margin_witness_verified": (
            certificate.get("position_margin_witness_verified") is True
        ),
        "symbol_scoped_redundancy_verified": (
            certificate.get("symbol_scoped_redundancy_verified") is True
        ),
        "reported_open_order_counts": allowed_counts,
        "completeness_contract_review_date": str(
            certificate.get("contract_review_date") or "UNKNOWN"
        )[:32],
    }
    for field in (
        "before_positions", "after_positions",
        "before_current_tpsl", "after_current_tpsl",
    ):
        count = result[field]
        result[field] = count if type(count) is int and count >= 0 else None
    # Account-wide completeness does not imply the existing position is
    # T100-owned. That needs a separate exchange+DB durable recovery proof.
    result["durable_nonflat_recovery_verified"] = False
    result["native_stop_protectiveness_independently_verified"] = False
    return result


async def _run(*, client: Any, timeout: float = 45.) -> dict[str, Any]:
    try:
        raw = await asyncio.wait_for(
            run_current_inventory_completeness_probe(
                ReadOnlyMexcProbe(client)
            ),
            timeout=timeout,
        )
        return shareable_current_inventory_report(raw)
    except Exception as exc:
        # Underlying HTTP exceptions may carry request and response content.
        # Keep only exception classes in operator shareable output.
        return shareable_current_inventory_report({
            "status": "CURRENT_INVENTORY_COMPLETENESS_DIAGNOSTIC_HALTED",
            "failure_type": type(exc).__name__,
        })


async def _main() -> None:
    controls = T100ExecutionControls.from_env()
    if (
        controls.mode != "paper"
        or controls.live_orders_enabled
        or not controls.kill_switch_engaged
    ):
        print(json.dumps({
            "status": "CURRENT_INVENTORY_READ_ONLY_MODE_REQUIRED",
            "live_authorization": False,
        }))
        raise SystemExit(2)
    key = os.getenv("MEXC_API_KEY") or ""
    secret = os.getenv("MEXC_API_SECRET") or ""
    if not key or not secret:
        print(json.dumps({
            "status": "CURRENT_INVENTORY_READ_CREDENTIALS_MISSING",
            "live_authorization": False,
        }))
        raise SystemExit(2)

    from app.mexc_trade import MexcTradeClient
    client = MexcTradeClient(
        "https://api.mexc.com",
        api_key=key, api_secret=secret,
    )
    try:
        report = await _run(client=client)
        print(json.dumps(report, indent=2, sort_keys=True))
        print("PRIVATE GET ONLY. NO ORDERS. LIVE TRADING DISABLED.")
    finally:
        await client.close()


def cli() -> None:
    parser = argparse.ArgumentParser(
        description="Non-atomic, redacted MEXC account inventory audit"
    )
    parser.parse_args()
    try:
        asyncio.run(_main())
    except SystemExit:
        raise
    except Exception as exc:
        print(json.dumps({
            "status": "CURRENT_INVENTORY_DIAGNOSTIC_FAILED",
            "failure_type": type(exc).__name__,
            "live_authorization": False,
        }))
        raise SystemExit(2) from None


if __name__ == "__main__":
    cli()
