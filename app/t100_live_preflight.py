"""T100 live readiness checks: no order submission, no DB writes.

Usage:
    python -m app.t100_live_preflight

Credentials, when available, come from MEXC_API_KEY and MEXC_API_SECRET
in the process environment, never from command-line flags.
"""
from __future__ import annotations

import asyncio
import json
import os
from typing import Any, TYPE_CHECKING

if TYPE_CHECKING:
    from app.mexc_trade import MexcTradeClient
from app.t100_live_guards import LIVE_ADAPTER_STATUS, T100ExecutionControls
from app.t100_live_readonly_probe import ReadOnlyMexcProbe


async def read_only_preflight(
    *,
    client: MexcTradeClient | None = None,
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


def main() -> None:
    result = asyncio.run(read_only_preflight())
    print(json.dumps(result, indent=2, sort_keys=True))
    print("NO ORDERS SENT. THIS CHECK DOES NOT ENABLE LIVE TRADING.")


if __name__ == "__main__":
    main()
