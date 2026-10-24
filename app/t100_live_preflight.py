"""T100 live readiness checks: no order submission, no DB writes.

Usage:
    python -m app.t100_live_preflight

Credentials, when available, come from MEXC_API_KEY and MEXC_API_SECRET
in the process environment, never from command-line flags.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
from typing import Any, TYPE_CHECKING

if TYPE_CHECKING:
    from app.mexc_trade import MexcTradeClient
from app.t100_live_guards import LIVE_ADAPTER_STATUS, T100ExecutionControls
from app.t100_live_readonly_probe import ReadOnlyMexcProbe
from app.t100_live_contract_evidence import summarize_inventory_schema
from app.t100_live_schema_assessment import assess_shadow_schema


async def read_only_preflight(
    *,
    client: MexcTradeClient | None = None,
    include_historical_stop_schema: bool = False,
) -> dict[str, Any]:
    controls = T100ExecutionControls.from_env()
    failures = list(controls.readiness_failures())
    result: dict[str, Any] = {
        "trader": "T100_225",
        "requested_mode": controls.mode,
        "current_production_adapter": "PAPER",
        "future_live_adapter": LIVE_ADAPTER_STATUS,
        "live_order_submission_possible": False,
        "read_only": True,
        "preflight_failures": failures,
        "exchange": "MEXC",
        "api_base_url": "https://api.mexc.com",
    }

    key, secret = os.getenv("MEXC_API_KEY"), os.getenv("MEXC_API_SECRET")
    if not key or not secret:
        result["credential_status"] = "MISSING; no private API calls made"
        return result

    owned = client is None
    if owned:
        # Avoid importing network dependencies during pure offline safety tests.
        from app.mexc_trade import MexcTradeClient
        client = MexcTradeClient("https://api.mexc.com", api_key=key, api_secret=secret)
    assert client is not None
    try:
        # No mutation method (_private_post, create, cancel, close, stop) is invoked.
        result["server_ping"] = await client.ping()
        asset = await client.usdt_asset()
        result["usdt_equity"] = float(asset.get("equity") or 0)
        result["usdt_available"] = await client.usdt_available_balance()
        result["position_mode"] = await client.position_mode()
        # The legacy open_positions()/open_stop_orders() helpers silently
        # discard malformed rows. Never infer an empty account from them.
        probe = ReadOnlyMexcProbe(client)
        positions = await probe.raw_open_positions()
        stops = await probe.raw_open_stops()
        # Shape evidence contains only allowlisted field names and types;
        # never IDs, symbols, quantities, balances or unknown key names.
        result["redacted_inventory_schema"] = summarize_inventory_schema(positions, stops)
        assessment_evidence = result["redacted_inventory_schema"]
        result["stop_schema_source"] = "open_orders"
        result["active_stop_schema_observed"] = bool(stops)
        result["historical_stop_schema_observed"] = False
        if include_historical_stop_schema and not stops:
            # Schema discovery only. MEXC's documented history response is a
            # list with requested page_num/page_size but no reported total,
            # therefore this single page can never certify completeness.
            historical_stops = await probe.raw_stop_page(1, 100, is_finished=1)
            result["redacted_historical_stop_schema_page1"] = summarize_inventory_schema(
                [], historical_stops
            )["stops"]
            if historical_stops:
                result["historical_stop_schema_observed"] = True
                assessment_evidence = {
                    "positions": result["redacted_inventory_schema"]["positions"],
                    "stops": result["redacted_historical_stop_schema_page1"],
                }
                result["stop_schema_source"] = "historical_finished_page1"
        result["shadow_schema_assessment"] = assess_shadow_schema(assessment_evidence)
        result["shadow_schema_assessment"]["stop_schema_source"] = result["stop_schema_source"]
        result["shadow_schema_assessment"]["active_stop_schema_observed"] = result[
            "active_stop_schema_observed"
        ]
        result["shadow_schema_assessment"]["historical_stop_schema_observed"] = result[
            "historical_stop_schema_observed"
        ]
        result["shadow_schema_assessment"]["active_stop_contract_verified"] = False
        result["open_futures_positions"] = len(positions)
        result["open_futures_stop_orders"] = len(stops)
        await client.refresh_contract_specs()
        result["public_contract_specs_loaded"] = len(client._spec_cache)

        if result["position_mode"] != 1:
            failures.append("hedge_position_mode_not_verified")
        if result["open_futures_positions"] != 0:
            failures.append("account_has_existing_positions")
        if result["open_futures_stop_orders"] != 0:
            failures.append("account_has_existing_stop_orders")
        if result["usdt_available"] <= 0:
            failures.append("no_available_futures_usdt")
        result["credential_status"] = "SIGNED_PRIVATE_READS_SUCCEEDED_UNCERTIFIED_SCHEMA"
        failures.append("venue_read_contract_and_pagination_not_certified")
    except Exception as exc:
        # Do not emit secrets, raw URLs, request bodies, or credentials.
        failures.append("private_or_public_read_failed")
        result["read_failure_type"] = type(exc).__name__
    finally:
        if owned:
            await client.close()
    return result


def shareable_contract_report(result: dict[str, Any]) -> dict[str, Any]:
    """Allowlist-only output for sharing schema evidence without account values."""
    return {
        "exchange": "MEXC",
        "read_only": True,
        "live_order_submission_possible": False,
        "credential_status": result.get("credential_status", "UNKNOWN"),
        "read_failure_type": result.get("read_failure_type"),
        "redacted_inventory_schema": result.get("redacted_inventory_schema"),
        "shadow_schema_assessment": result.get("shadow_schema_assessment"),
        "redacted_historical_stop_schema_page1": result.get(
            "redacted_historical_stop_schema_page1"
        ),
        "venue_contract_certified": False,
        "pagination_certified": False,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="Read-only T100 MEXC preflight")
    parser.add_argument("--schema-only", action="store_true",
                        help="Print only shareable redacted API shape evidence")
    args = parser.parse_args()
    result = asyncio.run(read_only_preflight(
        include_historical_stop_schema=args.schema_only
    ))
    print(json.dumps(shareable_contract_report(result) if args.schema_only else result,
                     indent=2, sort_keys=True))
    print("NO ORDERS SENT. THIS CHECK DOES NOT ENABLE LIVE TRADING.")


if __name__ == "__main__":
    main()
