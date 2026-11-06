"""Pure construction/reconciliation for a supervised T100 web-pilot drill.

A DRAFT is not a mutation permit. Never imports an HTTP client, submits
orders, saves keys, uses Render or authorizes trading. The proposed order
shape is based on MEXC current /api/v1/private/order/create documentation.
A later, separately approved sender must independently refresh evidence,
durably fence the unique externalOid BEFORE network POST, and use one POST
only. This module is intentionally not such a sender.
"""
from __future__ import annotations

import re
from decimal import Decimal, InvalidOperation
from typing import Any

from app.t100_live_native_stop_execution_audit import (
    assess_native_short_market_stop_row,
)

EXTERNAL_OID = re.compile(r"TPWEB[a-f0-9]{24}\Z")
MAX_NOTIONAL = Decimal("12.50")


class WebPilotPlanHalted(ValueError):
    pass


def _positive(raw: Any) -> Decimal:
    if type(raw) is bool or type(raw) not in (str, int, float, Decimal):
        raise WebPilotPlanHalted("INVALID_NUMERIC_INPUT")
    try:
        d = Decimal(str(raw))
    except (InvalidOperation, TypeError, ValueError):
        raise WebPilotPlanHalted("INVALID_NUMERIC_INPUT") from None
    if not d.is_finite() or d <= 0:
        raise WebPilotPlanHalted("INVALID_NUMERIC_INPUT")
    return d


def _exact_positive_id(raw: Any) -> str:
    if type(raw) not in (str, int):
        raise WebPilotPlanHalted("INVALID_ORDER_OR_POSITION_ID")
    value = str(raw)
    if re.fullmatch(r"[1-9][0-9]*", value) is None:
        raise WebPilotPlanHalted("INVALID_ORDER_OR_POSITION_ID")
    return value


def draft_entry_request(preview: Any, *, external_oid: str) -> dict[str, Any]:
    """For isolated offline review ONLY, cannot authorize a venue POST.

    A fresh preview must be acquired in the same future execution session;
    this pure draft cannot attest its age or the account bound to it.
    """
    if type(preview) is not dict or type(external_oid) is not str:
        raise WebPilotPlanHalted("PREVIEW_OR_ORIGIN_INVALID")
    required = {
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
        "entry_allowed": False,
        "live_authorization": False,
        "native_stop_protectiveness_verified": False,
        "exchange_atomicity_verified": False,
        "loss_cap_guaranteed": False,
        "mexc_orders_sent": 0,
    }
    if any(
        type(preview.get(k)) is not type(v) or preview.get(k) != v
        for k, v in required.items()
    ) or EXTERNAL_OID.fullmatch(external_oid) is None:
        raise WebPilotPlanHalted("PREVIEW_OR_ORIGIN_NOT_ELIGIBLE")
    price = _positive(preview.get("reference_last_price"))
    stop = _positive(preview.get("embedded_stop_loss"))
    notional = _positive(preview.get("indicative_notional_usdt"))
    if (
        notional > MAX_NOTIONAL
        or stop <= price or stop > price * Decimal("1.02")
    ):
        raise WebPilotPlanHalted("DRAFT_STOP_OR_NOTIONAL_OUT_OF_BOUNDS")
    return {
        "symbol": "BTC_USDT",
        "price": str(price),
        "vol": 1,
        "leverage": 1,
        "side": 3,
        "type": 5,
        "openType": 1,
        "externalOid": external_oid,
        "positionMode": 1,
        "stopLossPrice": str(stop),
        "lossTrend": 1,
    }


def assess_post_entry_snapshot(
    *, order: Any, positions: Any, stops: Any, draft: Any,
) -> dict[str, Any]:
    """GET-only static assessor. Strict failure is a manual emergency alert.

    It is not proof of venue stop execution nor exchange atomicity. Full
    bookended snapshot completeness and a durable origin remain necessary.
    """
    result = {
        "status": "PILOT_ENTRY_EVIDENCE_UNVERIFIED",
        "entry_order_and_position_matched": False,
        "active_native_stop_fields_observed": False,
        "stop_execution_certified": False,
        "account_wide_completeness_certified": False,
        "exchange_atomicity_certified": False,
        "live_authorization": False,
        "entry_allowed": False,
    }
    if (
        type(order) is not dict or type(draft) is not dict
        or type(positions) is not list or type(stops) is not list
        or any(type(x) is not dict for x in positions + stops)
        or len(positions) != 1 or len(stops) != 1
    ):
        result["status"] = "ENTRY_POSITION_OR_STOP_NOT_UNIQUE"
        return result
    try:
        orig = draft.get("externalOid")
        if type(orig) is not str or EXTERNAL_OID.fullmatch(orig) is None:
            raise WebPilotPlanHalted("ORIGIN_INVALID")
        order_id = _exact_positive_id(order.get("orderId"))
        position_id = _exact_positive_id(order.get("positionId"))
        position = positions[0]
        position_id_get = _exact_positive_id(position.get("positionId"))
        volume = _positive(position.get("holdVol"))
        if (
            order.get("externalOid") != orig
            or order.get("symbol") != "BTC_USDT"
            or type(order.get("side")) is not int or order["side"] != 3
            or type(order.get("state")) is not int or order["state"] != 3
            or type(order.get("openType")) is not int or order["openType"] != 1
            or type(order.get("positionMode")) is not int or order["positionMode"] != 1
            or _positive(order.get("dealVol")) != Decimal(1)
            or _positive(order.get("vol")) != Decimal(1)
            or position_id != position_id_get
            or position.get("symbol") != "BTC_USDT"
            or type(position.get("state")) is not int or position["state"] != 1
            or type(position.get("positionType")) is not int or position["positionType"] != 2
            or type(position.get("openType")) is not int or position["openType"] != 1
            or type(position.get("leverage")) is not int or position["leverage"] != 1
            or volume != Decimal(1)
        ):
            result["status"] = "PILOT_ENTRY_OR_POSITION_IDENTITY_UNVERIFIED"
            return result
        _ = order_id
        result["entry_order_and_position_matched"] = True
        stop_audit = assess_native_short_market_stop_row(
            stop=stops[0],
            symbol="BTC_USDT",
            position_id=position_id,
            position_quantity=Decimal(1),
            maximum_short_stop_price=_positive(draft.get("stopLossPrice")),
        )
        if not stop_audit.active_native_full_volume_market_sl_observed:
            result["status"] = stop_audit.status
            return result
        result["active_native_stop_fields_observed"] = True
        result["status"] = "NATIVE_STOP_FIELDS_OBSERVED_STILL_NONCERTIFYING"
        return result
    except WebPilotPlanHalted:
        result["status"] = "PILOT_ENTRY_EVIDENCE_INVALID"
        return result
