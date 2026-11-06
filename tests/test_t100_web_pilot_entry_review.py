"""Offline-only unit tests for the T100 one-contract entry and GET review."""
from copy import deepcopy

import pytest

from app.t100_web_pilot_entry_review import (
    WebPilotPlanHalted,
    assess_post_entry_snapshot,
    draft_entry_request,
)

OID = "TPWEB" + "0123456789abcdef01234567"


def preview():
    return {
        "status": "GET_ONLY_PREVIEW_READY_NOT_AUTHORIZED",
        "credential_scope": "T100_WEB_PILOT_SUBACCOUNT",
        "inventory_status": "CURRENT_ACCOUNT_INVENTORY_COMPLETENESS_VERIFIED_NONATOMIC",
        "symbol": "BTC_USDT",
        "direction": "SHORT",
        "contracts": 1,
        "margin": "ISOLATED",
        "leverage": "1x",
        "market_order": True,
        "stop_reference": "LATEST_PRICE",
        "notional_ceiling_usdt": "12.50",
        "available_futures_ge_12_50": True,
        "reference_last_price": "75875.2",
        "embedded_stop_loss": "76634.0",
        "indicative_notional_usdt": "7.5875",
        "entry_allowed": False,
        "live_authorization": False,
        "native_stop_protectiveness_verified": False,
        "exchange_atomicity_verified": False,
        "loss_cap_guaranteed": False,
        "mexc_orders_sent": 0,
    }


def draft():
    return draft_entry_request(preview(), external_oid=OID)


def order():
    return {
        "orderId": "123456", "positionId": "98765",
        "externalOid": OID, "symbol": "BTC_USDT",
        "side": 3, "state": 3, "openType": 1,
        "positionMode": 1, "dealVol": 1, "vol": 1,
    }


def position():
    return {
        "positionId": "98765", "symbol": "BTC_USDT", "state": 1,
        "positionType": 2, "openType": 1, "leverage": 1,
        "holdVol": 1,
    }


def stop():
    return {
        "id": "4567", "positionId": "98765", "symbol": "BTC_USDT",
        "orderId": 0, "triggerSide": 0, "errorCode": 0,
        "state": 1, "isFinished": 0, "positionType": 2,
        "stopLossType": 0, "stopLossReverse": 2, "lossTrend": 1,
        "stopLossPrice": "76634.0", "volType": 2,
        "profitLossVolType": "SAME",
    }


def assess(**overrides):
    values = {
        "order": order(),
        "positions": [position()],
        "stops": [stop()],
        "draft": draft(),
    }
    values.update(overrides)
    return assess_post_entry_snapshot(**values)


def test_draft_exact_minimum_short_with_embedded_stop():
    expected = {
        "symbol": "BTC_USDT",
        "price": "75875.2",
        "vol": 1,
        "leverage": 1,
        "side": 3,
        "type": 5,
        "openType": 1,
        "externalOid": OID,
        "positionMode": 1,
        "stopLossPrice": "76634.0",
        "lossTrend": 1,
    }
    assert draft() == expected
    assert "takeProfitPrice" not in draft()
    assert "stopLossType" not in draft()


@pytest.mark.parametrize("field,value", [
    ("status", "READY_TO_SEND"),
    ("credential_scope", "T100_V5_PILOT_SUBACCOUNT"),
    ("inventory_status", "UNKNOWN"),
    ("symbol", "ETH_USDT"),
    ("direction", "LONG"),
    ("contracts", 2),
    ("margin", "CROSS"),
    ("leverage", "2x"),
    ("entry_allowed", True),
    ("live_authorization", True),
    ("native_stop_protectiveness_verified", True),
    ("mexc_orders_sent", 1),
    ("indicative_notional_usdt", "12.5001"),
    ("embedded_stop_loss", "75875.2"),
    ("embedded_stop_loss", "77000"),
    ("embedded_stop_loss", "1e309"),
    ("reference_last_price", "NaN"),
])
def test_draft_rejects_unsafe_or_mutated_preview(field, value):
    changed = preview()
    changed[field] = value
    with pytest.raises(WebPilotPlanHalted):
        draft_entry_request(changed, external_oid=OID)


@pytest.mark.parametrize("oid", [
    "TPWEBabc", "TPWEB" + "g"*24, "OTHER" + "a"*24,
    "TPWEB" + "0"*23, "TPWEB" + "0"*25,
    "TPWEB" + "0"*23 + "/",
])
def test_draft_rejects_invalid_origin(oid):
    with pytest.raises(WebPilotPlanHalted):
        draft_entry_request(preview(), external_oid=oid)


def test_matching_position_and_active_whole_market_stop_still_non_authorizing():
    result = assess()
    assert result["entry_order_and_position_matched"]
    assert result["active_native_stop_fields_observed"]
    assert result["status"] == "NATIVE_STOP_FIELDS_OBSERVED_STILL_NONCERTIFYING"
    assert not result["stop_execution_certified"]
    assert not result["account_wide_completeness_certified"]
    assert not result["exchange_atomicity_certified"]
    assert not result["live_authorization"]


@pytest.mark.parametrize("field,value", [
    ("externalOid", "OTHER"), ("symbol", "ETH_USDT"),
    ("side", 1), ("state", 2), ("openType", 2),
    ("positionMode", 2), ("dealVol", 0), ("vol", 2),
    ("positionId", "22"), ("orderId", "0"),
])
def test_order_identity_changes_fail_closed(field, value):
    r = order()
    r[field] = value
    result = assess(order=r)
    assert not result["active_native_stop_fields_observed"]
    assert not result["entry_order_and_position_matched"]


@pytest.mark.parametrize("field,value", [
    ("positionId", "111"), ("symbol", "ETH_USDT"),
    ("state", 3), ("positionType", 1), ("openType", 2),
    ("leverage", 3), ("holdVol", 2),
])
def test_position_changes_fail_closed(field, value):
    p = position()
    p[field] = value
    result = assess(positions=[p])
    assert not result["entry_order_and_position_matched"]


@pytest.mark.parametrize("field,value", [
    ("orderId", "123456"),  # attached order is not independent position protection
    ("state", 2), ("isFinished", 1),
    ("stopLossType", 1), ("stopLossReverse", 1),
    ("stopLossPrice", "78000"), ("volType", 0),
    ("lossTrend", 2),
])
def test_stop_not_independently_proven(field, value):
    s = stop()
    s[field] = value
    result = assess(stops=[s])
    assert result["entry_order_and_position_matched"]
    assert not result["active_native_stop_fields_observed"]
    assert not result["live_authorization"]


def test_ambiguous_multiple_positions_or_stops_fail_closed():
    assert assess(positions=[position(), position()])["status"] == (
        "ENTRY_POSITION_OR_STOP_NOT_UNIQUE"
    )
    assert assess(stops=[stop(), stop()])["status"] == (
        "ENTRY_POSITION_OR_STOP_NOT_UNIQUE"
    )
