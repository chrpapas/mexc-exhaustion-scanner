"""Offline-only T100 web pilot GET preview gates; no MEXC POST."""
import asyncio
import time
from unittest.mock import AsyncMock, patch

import pytest

from app.t100_web_pilot_dry_run_cli import (
    PilotDryRunGetOnlyClient,
    PilotDryRunHalted,
    check,
    normalized_position_mode,
    one_contract_metadata,
    one_usdt_asset,
    propose_one_contract,
)

NOW = 1770000000000


def contract():
    return {
        "symbol": "BTC_USDT", "state": 0, "apiAllowed": True,
        "futureType": 1, "quoteCoin": "USDT", "settleCoin": "USDT",
        "positionOpenType": 3, "contractSize": "0.0001",
        "priceUnit": "0.1", "volUnit": 1, "minVol": 1,
        "maxVol": 50000, "minLeverage": 1, "maxLeverage": 20,
    }


def ticker():
    return {
        "symbol": "BTC_USDT", "lastPrice": "100000.0",
        "ask1": "100002.0", "timestamp": NOW - 300,
    }


def preview(**kwargs):
    values = dict(
        contract=contract(), ticker=ticker(), available="49.9",
        position_mode=1, now_ms=NOW,
    )
    values.update(kwargs)
    return propose_one_contract(**values)


def test_one_contract_isolated_short_stop_above_market_no_authorization():
    result = preview()
    assert result["status"] == "GET_ONLY_PREVIEW_READY_NOT_AUTHORIZED"
    assert result["contracts"] == 1
    assert result["leverage"] == "1x"
    assert result["margin"] == "ISOLATED"
    assert result["embedded_stop_loss"] == "101000.0"
    assert result["indicative_notional_usdt"] == "10.0002"
    assert result["mexc_orders_sent"] == 0
    assert not result["live_authorization"]
    assert not result["entry_allowed"]
    assert not result["loss_cap_guaranteed"]


@pytest.mark.parametrize("field,value", [
    ("apiAllowed", False), ("state", 4), ("futureType", 2),
    ("positionOpenType", 2), ("symbol", "ETH_USDT"),
    ("settleCoin", "BTC"),
])
def test_contract_gates_fail_closed(field, value):
    c = contract()
    c[field] = value
    with pytest.raises(PilotDryRunHalted):
        preview(contract=c)


@pytest.mark.parametrize("field,value", [
    ("minVol", 2), ("volUnit", 3), ("minLeverage", 2),
])
def test_contract_volume_or_one_x_invalid(field, value):
    c = contract()
    c[field] = value
    with pytest.raises(PilotDryRunHalted):
        preview(contract=c)


def test_no_hedge_mode_blocks():
    with pytest.raises(PilotDryRunHalted, match="HEDGE_MODE"):
        preview(position_mode=2)


def test_insufficient_balance_blocks():
    with pytest.raises(PilotDryRunHalted, match="AVAILABLE"):
        preview(available="1")


def test_notional_limit_blocks_when_btc_expensive():
    t = ticker()
    t["lastPrice"] = "130000"
    t["ask1"] = "130001"
    with pytest.raises(PilotDryRunHalted, match="CAP"):
        preview(ticker=t)


def test_stale_quote_blocks():
    t = ticker()
    t["timestamp"] = NOW - 50000
    with pytest.raises(PilotDryRunHalted, match="STALE"):
        preview(ticker=t)


def test_ask_market_dislocation_blocks():
    t = ticker()
    t["ask1"] = "101000"
    with pytest.raises(PilotDryRunHalted, match="QUOTE"):
        preview(ticker=t)


def test_invalid_numeric_data_blocks():
    c = contract()
    c["contractSize"] = "NaN"
    with pytest.raises(PilotDryRunHalted):
        preview(contract=c)


def test_duplicate_account_asset_and_metadata_fail_closed():
    with pytest.raises(PilotDryRunHalted):
        one_usdt_asset([
            {"currency": "USDT", "availableBalance": "49.9"},
            {"currency": "USDT", "availableBalance": "49.9"},
        ])
    with pytest.raises(PilotDryRunHalted):
        one_contract_metadata([contract(), contract()])


def test_mode_parser_requires_explicit_int():
    assert normalized_position_mode(1) == 1
    assert normalized_position_mode({"positionMode": 1}) == 1
    assert normalized_position_mode("1") is None
    assert normalized_position_mode({"positionMode": True}) is None


@pytest.mark.asyncio
async def test_route_allowlist_rejects_order_paths_without_network():
    raw = type("Raw", (), {
        "_private_get": AsyncMock(return_value=[]),
        "_public_get": AsyncMock(return_value={}),
    })()
    wrapped = PilotDryRunGetOnlyClient(raw)
    assert not hasattr(wrapped, "_private_post")
    assert not hasattr(wrapped, "_post")
    with pytest.raises(PilotDryRunHalted):
        await wrapped._private_get("/api/v1/private/order/create")
    with pytest.raises(PilotDryRunHalted):
        await wrapped._public_get("/api/v1/contract/detail", {"symbol": "ETH_USDT"})
    with pytest.raises(PilotDryRunHalted):
        await wrapped._private_get("/api/v1/private/position/position_mode",
                                   {"symbol": "BTC_USDT"})
    raw._private_get.assert_not_awaited()
    raw._public_get.assert_not_awaited()


@pytest.mark.asyncio
async def test_get_only_preflight_never_sends_order():
    raw = type("Raw", (), {
        "_private_get": AsyncMock(),
        "_public_get": AsyncMock(),
    })()
    raw._public_get.side_effect = [contract(), ticker()]
    raw._private_get.side_effect = [
        [{"currency": "USDT", "availableBalance": "49.9"}],
        1,
    ]
    inventory = {
        "status": "CURRENT_ACCOUNT_INVENTORY_COMPLETENESS_VERIFIED_NONATOMIC",
        "account_reconciled": True,
        "account_wide_completeness_verified": True,
        "after_positions": 0, "after_current_tpsl": 0,
        "reported_open_order_counts": {
            "sumCount": 0, "stopOrderCount": 0, "limitOrderCount": 0,
            "planOrderCount": 0, "trackOrderCount": 0,
        },
    }
    with (
        patch("app.t100_web_pilot_dry_run_cli.run_inventory",
              new=AsyncMock(return_value=inventory)),
        patch("app.t100_web_pilot_dry_run_cli.time.time",
              return_value=NOW / 1000),
    ):
        result = await check(PilotDryRunGetOnlyClient(raw))
    assert result["status"] == "GET_ONLY_PREVIEW_READY_NOT_AUTHORIZED"
    assert result["credential_scope"] == "T100_WEB_PILOT_SUBACCOUNT"
    assert raw._public_get.await_count == 2
    assert raw._private_get.await_count == 2
    assert all(call.args[0].startswith("/api/v1/private/")
               for call in raw._private_get.await_args_list)


@pytest.mark.asyncio
async def test_nonflat_inventory_never_reads_further_data():
    raw = type("Raw", (), {
        "_private_get": AsyncMock(),
        "_public_get": AsyncMock(),
    })()
    with patch("app.t100_web_pilot_dry_run_cli.run_inventory",
               new=AsyncMock(return_value={"account_reconciled": False})):
        with pytest.raises(PilotDryRunHalted):
            await check(PilotDryRunGetOnlyClient(raw))
    raw._private_get.assert_not_awaited()
    raw._public_get.assert_not_awaited()
