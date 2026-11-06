"""T100 new-web-subaccount BTC_USDT one-contract PREVIEW.

No POST/order method or send switch. Explicitly non-authorizing. Reads the
dedicated Mac Keychain, bookended account inventory and venue metadata.
Output intentionally excludes identities, balances, raw API responses and keys.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import time
from decimal import Decimal, InvalidOperation, ROUND_CEILING
from typing import Any

from app.t100_live_current_inventory_cli import (
    InventoryGetOnlyClient, PilotKeychainUnavailable,
    _run as run_inventory,
    read_pilot_keychain_credentials,
)

SYMBOL = "BTC_USDT"
MAX_NOTIONAL = Decimal("12.50")
MIN_AVAILABLE = Decimal("12.50")
STOP_MARKUP = Decimal("1.01")
MAX_TICKER_AGE_MS = 30000


class PilotDryRunHalted(ValueError):
    pass


def decimal(value: Any) -> Decimal:
    if type(value) not in (str, float, int, Decimal):
        raise PilotDryRunHalted("INVALID_NUMERIC_DATA")
    try:
        result = Decimal(str(value))
    except (InvalidOperation, ValueError):
        raise PilotDryRunHalted("INVALID_NUMERIC_DATA") from None
    if not result.is_finite() or result <= 0:
        raise PilotDryRunHalted("INVALID_NUMERIC_DATA")
    return result


def propose_one_contract(
    *, contract: dict[str, Any], ticker: dict[str, Any],
    available: Any, position_mode: Any, now_ms: int,
) -> dict[str, Any]:
    """Pure fail-closed quote/model gate. Never a venue order authorization."""
    if type(contract) is not dict or type(ticker) is not dict:
        raise PilotDryRunHalted("VENUE_METADATA_UNVERIFIED")
    if (
        contract.get("symbol") != SYMBOL
        or contract.get("state") != 0
        or contract.get("apiAllowed") is not True
        or contract.get("futureType") != 1
        or contract.get("quoteCoin") != "USDT"
        or contract.get("settleCoin") != "USDT"
        or contract.get("positionOpenType") not in (1, 3)
        or ticker.get("symbol") != SYMBOL
    ):
        raise PilotDryRunHalted("CONTRACT_METADATA_NOT_ELIGIBLE")
    if type(position_mode) is not int or position_mode != 1:
        raise PilotDryRunHalted("HEDGE_MODE_NOT_VERIFIED")
    contract_size = decimal(contract.get("contractSize"))
    tick = decimal(contract.get("priceUnit"))
    unit = decimal(contract.get("volUnit"))
    min_vol = decimal(contract.get("minVol"))
    max_vol = decimal(contract.get("maxVol"))
    minimum_leverage = decimal(contract.get("minLeverage"))
    maximum_leverage = decimal(contract.get("maxLeverage"))
    if (
        min_vol > 1 or max_vol < 1
        or (Decimal(1) / unit) % 1 != 0
        or minimum_leverage > 1 or maximum_leverage < 1
    ):
        raise PilotDryRunHalted("ONE_CONTRACT_ISOLATED_1X_UNVERIFIED")
    last = decimal(ticker.get("lastPrice"))
    ask = decimal(ticker.get("ask1"))
    age_value = ticker.get("timestamp")
    if type(age_value) is not int or type(now_ms) is not int:
        raise PilotDryRunHalted("TICKER_TIMESTAMP_UNVERIFIED")
    age = now_ms - age_value
    if age < -5000 or age > MAX_TICKER_AGE_MS:
        raise PilotDryRunHalted("STALE_TICKER")
    if (ask / last) > Decimal("1.005") or (ask / last) < Decimal("0.995"):
        raise PilotDryRunHalted("QUOTE_SPREAD_OR_PRICE_DRIFT")
    notional = ask * contract_size
    if notional > MAX_NOTIONAL or decimal(available) < MIN_AVAILABLE:
        raise PilotDryRunHalted("PILOT_NOTIONAL_OR_AVAILABLE_CAP_FAILED")
    stop = (last * STOP_MARKUP / tick).to_integral_value(
        rounding=ROUND_CEILING
    ) * tick
    if stop <= ask:
        raise PilotDryRunHalted("SHORT_STOP_NOT_ABOVE_MARKET")
    if (stop - ask) * contract_size > Decimal("0.25"):
        raise PilotDryRunHalted("INDICATIVE_STOP_DISTANCE_OVER_CAP")
    # These are indicative figures, NOT a maximum guaranteed loss. Stop
    # execution may fail or slip, and market execution price is unknown.
    return {
        "status": "GET_ONLY_PREVIEW_READY_NOT_AUTHORIZED",
        "symbol": SYMBOL,
        "direction": "SHORT",
        "contracts": 1,
        "margin": "ISOLATED",
        "leverage": "1x",
        "market_order": True,
        "embedded_stop_loss": str(stop),
        "stop_reference": "LATEST_PRICE",
        "reference_last_price": str(last),
        "indicative_notional_usdt": str(notional.quantize(Decimal("0.0001"))),
        "notional_ceiling_usdt": str(MAX_NOTIONAL),
        "available_futures_ge_12_50": True,
        "loss_cap_guaranteed": False,
        "entry_allowed": False,
        "live_authorization": False,
        "exchange_atomicity_verified": False,
        "native_stop_protectiveness_verified": False,
        "mexc_orders_sent": 0,
        "render_deployments": 0,
    }


def one_usdt_asset(assets: Any) -> Any:
    if type(assets) is not list or any(type(a) is not dict for a in assets):
        raise PilotDryRunHalted("ASSET_RESPONSE_UNVERIFIED")
    selected = [a for a in assets if a.get("currency") == "USDT"]
    if len(selected) != 1:
        raise PilotDryRunHalted("USDT_ASSET_NOT_UNIQUE")
    return selected[0].get("availableBalance")


def one_contract_metadata(info: Any) -> dict[str, Any]:
    rows = info if type(info) is list else [info]
    matches = [r for r in rows if type(r) is dict and r.get("symbol") == SYMBOL]
    if len(matches) != 1:
        raise PilotDryRunHalted("CONTRACT_METADATA_NOT_UNIQUE")
    return matches[0]


def normalized_position_mode(raw: Any) -> int | None:
    if type(raw) is int:
        return raw
    if type(raw) is dict and type(raw.get("positionMode")) is int:
        return raw["positionMode"]
    return None


async def check(client: Any) -> dict[str, Any]:
    inventory = await run_inventory(client=InventoryGetOnlyClient(client))
    if (
        inventory.get("account_reconciled") is not True
        or inventory.get("account_wide_completeness_verified") is not True
        or inventory.get("after_positions") != 0
        or inventory.get("after_current_tpsl") != 0
        or any(x != 0 for x in inventory.get("reported_open_order_counts", {}).values())
    ):
        raise PilotDryRunHalted("NEW_SUBACCOUNT_CURRENT_INVENTORY_NOT_FLAT")
    info = await client._public_get("/api/v1/contract/detail", {"symbol": SYMBOL})
    ticker = await client._public_get("/api/v1/contract/ticker", {"symbol": SYMBOL})
    assets = await client._private_get("/api/v1/private/account/assets")
    mode = await client._private_get("/api/v1/private/position/position_mode")
    if type(ticker) is list:
        tickers = [x for x in ticker if type(x) is dict and x.get("symbol") == SYMBOL]
        if len(tickers) != 1:
            raise PilotDryRunHalted("TICKER_NOT_UNIQUE")
        ticker = tickers[0]
    result = propose_one_contract(
        contract=one_contract_metadata(info),
        ticker=ticker,
        available=one_usdt_asset(assets),
        position_mode=normalized_position_mode(mode),
        now_ms=int(time.time() * 1000),
    )
    result["inventory_status"] = inventory["status"]
    result["credential_scope"] = "T100_WEB_PILOT_SUBACCOUNT"
    return result


async def _main() -> None:
    if (
        os.environ.get("T100_EXECUTION_MODE") != "paper"
        or os.environ.get("T100_LIVE_ORDER_API_ENABLED") != "false"
        or os.environ.get("T100_LIVE_KILL_SWITCH") != "true"
    ):
        raise PilotDryRunHalted("PAPER_KILL_ENV_REQUIRED")
    try:
        key, secret = read_pilot_keychain_credentials(web_pilot=True)
    except PilotKeychainUnavailable as exc:
        raise PilotDryRunHalted(str(exc)) from None
    from app.mexc_trade import MexcTradeClient
    client = MexcTradeClient(
        "https://api.mexc.com", api_key=key, api_secret=secret,
    )
    try:
        output = await asyncio.wait_for(check(client), timeout=50)
    finally:
        await client.close()
    print(json.dumps(output, indent=2, sort_keys=True))
    print("GET ONLY. NO ORDERS, NO LIVE AUTHORIZATION.")


def main() -> None:
    parser = argparse.ArgumentParser(description="GET-only T100 web pilot dry-run")
    parser.parse_args()  # Unknown options, including --send, fail closed.
    try:
        asyncio.run(_main())
    except Exception as exc:
        # Network and venue errors may carry private request data. Redact.
        error = str(exc) if isinstance(exc, PilotDryRunHalted) else type(exc).__name__
        print(json.dumps({
            "status": "GET_ONLY_PILOT_PREFLIGHT_HALTED",
            "reason": error,
            "credential_scope": "T100_WEB_PILOT_SUBACCOUNT",
            "entry_allowed": False,
            "live_authorization": False,
            "mexc_orders_sent": 0,
        }, sort_keys=True))
        raise SystemExit(2) from None


if __name__ == "__main__":
    main()
