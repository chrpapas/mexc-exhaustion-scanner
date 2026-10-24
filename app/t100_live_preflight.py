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
import time
from typing import Any, TYPE_CHECKING

if TYPE_CHECKING:
    from app.mexc_trade import MexcTradeClient
from app.t100_live_guards import LIVE_ADAPTER_STATUS, T100ExecutionControls
from app.t100_live_readonly_probe import ReadOnlyMexcProbe
from app.t100_live_contract_evidence import summarize_inventory_schema
from app.t100_live_schema_assessment import assess_shadow_schema
from app.t100_live_consistency import InventoryRead, double_read_raw_inventory
from app.t100_live_active_tpsl import assess_active_tpsl_semantics
from app.t100_live_stop_transition import assess_recent_finished_tpsl_transition
from app.t100_live_funding_probe import collect_observed_position_funding

PREFLIGHT_DIAGNOSTIC_VERSION = "2026-10-24-stop-transition-v1"


async def read_only_preflight(
    *,
    client: MexcTradeClient | None = None,
    include_historical_stop_schema: bool = False,
    include_raw_consistency: bool = False,
    include_funding_probe: bool = False,
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
            result["recent_finished_tpsl_transition"] = (
                assess_recent_finished_tpsl_transition(
                    positions, historical_stops
                )
            )
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
        result["active_tpsl_semantics"] = assess_active_tpsl_semantics(
            positions, stops
        )
        if positions and not stops:
            result["t100_native_protection_presence"] = (
                "POSITION_WITHOUT_OBSERVED_CURRENT_TPSL"
            )
            failures.append("t100_position_without_observed_native_tpsl")
        elif stops and not positions:
            result["t100_native_protection_presence"] = (
                "CURRENT_TPSL_WITHOUT_OBSERVED_POSITION"
            )
            failures.append("t100_current_tpsl_without_observed_position")
        elif positions and stops:
            result["t100_native_protection_presence"] = (
                "POSITION_AND_CURRENT_TPSL_PRESENT_LINKAGE_UNVERIFIED"
            )
        else:
            result["t100_native_protection_presence"] = (
                "NO_POSITION_OR_CURRENT_TPSL_OBSERVED_COMPLETENESS_UNVERIFIED"
            )

        if include_funding_probe:
            if len(positions) == 1:
                pid_raw = positions[0].get("positionId") or positions[0].get("id")
                try:
                    result["funding_observed_position_probe"] = (
                        await collect_observed_position_funding(
                            probe, position_id=str(pid_raw or "")
                        )
                    )
                    result["funding_observed_position_probe"]["status"] = (
                        "PAGINATION_VERIFIED_SIGN_SEMANTICS_UNCERTIFIED"
                    )
                except Exception as exc:
                    result["funding_observed_position_probe"] = {
                        "status": "FUNDING_READ_CONTRACT_UNVERIFIED",
                        "failure_type": type(exc).__name__,
                        "pagination_verified_for_observed_position": False,
                        "funding_sign_semantics_verified": False,
                        "account_wide_funding_completeness_verified": False,
                    }
            else:
                result["funding_observed_position_probe"] = {
                    "status": "NOT_RUN_REQUIRES_EXACTLY_ONE_OBSERVED_POSITION",
                    "pagination_verified_for_observed_position": False,
                    "funding_sign_semantics_verified": False,
                    "account_wide_funding_completeness_verified": False,
                }

        if include_raw_consistency:
            last_observed_ms = -1

            async def _raw_inventory_read() -> InventoryRead:
                nonlocal last_observed_ms
                p = await probe.raw_open_positions()
                s = await probe.raw_open_stops()
                observed_ms = time.monotonic_ns() // 1_000_000
                # Two very fast fake/local reads can share one millisecond.
                # Preserve strict observation order without hiding a real
                # multi-millisecond delay: only bump an equal timestamp by 1.
                if observed_ms <= last_observed_ms:
                    observed_ms = last_observed_ms + 1
                last_observed_ms = observed_ms
                return InventoryRead(
                    p, s, observed_ms,
                    positions_complete=False, stops_complete=False,
                )
            try:
                evidence = await double_read_raw_inventory(_raw_inventory_read)
                result["raw_inventory_double_read_consistent"] = True
                result["raw_inventory_double_read_mode"] = evidence.mode
                result["raw_inventory_completeness_verified"] = False
            except Exception as exc:
                result["raw_inventory_double_read_consistent"] = False
                result["raw_inventory_consistency_failure_type"] = type(exc).__name__
                result["raw_inventory_completeness_verified"] = False
                failures.append("raw_inventory_double_read_unverified")

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
        "diagnostic_version": PREFLIGHT_DIAGNOSTIC_VERSION,
        "read_only": True,
        "live_order_submission_possible": False,
        "credential_status": result.get("credential_status", "UNKNOWN"),
        "read_failure_type": result.get("read_failure_type"),
        "redacted_inventory_schema": result.get("redacted_inventory_schema"),
        "shadow_schema_assessment": result.get("shadow_schema_assessment"),
        "redacted_historical_stop_schema_page1": result.get(
            "redacted_historical_stop_schema_page1"
        ),
        "recent_finished_tpsl_transition": result.get(
            "recent_finished_tpsl_transition"
        ),
        "funding_observed_position_probe": result.get(
            "funding_observed_position_probe"
        ),
        "raw_inventory_double_read_consistent": result.get(
            "raw_inventory_double_read_consistent"
        ),
        "t100_native_protection_presence": result.get(
            "t100_native_protection_presence"
        ),
        "active_tpsl_semantics": result.get("active_tpsl_semantics"),
        "raw_inventory_completeness_verified": result.get(
            "raw_inventory_completeness_verified", False
        ),
        "venue_contract_certified": False,
        "pagination_certified": False,
    }


async def _run_cli_preflight(*, schema_only: bool) -> dict[str, Any]:
    """Bound the entire operator-local diagnostic, not just each HTTP call."""
    try:
        return await asyncio.wait_for(
            read_only_preflight(
                include_historical_stop_schema=schema_only,
                include_raw_consistency=schema_only,
                include_funding_probe=schema_only,
            ),
            timeout=45.0,
        )
    except asyncio.TimeoutError:
        return {
            "exchange": "MEXC",
            "read_only": True,
            "live_order_submission_possible": False,
            "credential_status": "READ_ONLY_PREFLIGHT_TIMED_OUT",
            "read_failure_type": "TimeoutError",
            "preflight_failures": ["overall_read_only_preflight_timeout"],
        }


def main() -> None:
    parser = argparse.ArgumentParser(description="Read-only T100 MEXC preflight")
    parser.add_argument("--schema-only", action="store_true",
                        help="Print only shareable redacted API shape evidence")
    args = parser.parse_args()
    result = asyncio.run(_run_cli_preflight(schema_only=args.schema_only))
    print(json.dumps(shareable_contract_report(result) if args.schema_only else result,
                     indent=2, sort_keys=True))
    print("NO ORDERS SENT. THIS CHECK DOES NOT ENABLE LIVE TRADING.")


if __name__ == "__main__":
    main()
