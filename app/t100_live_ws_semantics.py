"""Fail-closed semantic checks for documented MEXC private WS payloads.

Pinned documentation review: 2026-10-26.
Channels:
- push.personal.position
- push.personal.stop.planorder
- push.personal.order.deal

These checks validate payload shape and internal semantics only. They do not
claim that a WS event caused or fully represents the authoritative exchange
state. REST correlation remains a separate requirement.
"""
from __future__ import annotations

from decimal import Decimal, InvalidOperation
from typing import Any

POSITION_CHANNEL = "push.personal.position"
STOP_CHANNEL = "push.personal.stop.planorder"
FILL_CHANNEL = "push.personal.order.deal"
SUPPORTED_CHANNELS = frozenset({
    POSITION_CHANNEL,
    STOP_CHANNEL,
    FILL_CHANNEL,
})


def _integer(raw: Any) -> int | None:
    if isinstance(raw, bool):
        return None
    try:
        value = int(str(raw))
    except (TypeError, ValueError):
        return None
    return value if str(value) == str(raw) else None


def _decimal(raw: Any) -> Decimal | None:
    try:
        value = Decimal(str(raw))
    except (InvalidOperation, TypeError, ValueError):
        return None
    return value if value.is_finite() else None


def _positive_id(raw: Any) -> bool:
    value = _integer(raw)
    return value is not None and value > 0


def _nonnegative_decimal(raw: Any) -> bool:
    value = _decimal(raw)
    return value is not None and value >= 0


def _positive_decimal(raw: Any) -> bool:
    value = _decimal(raw)
    return value is not None and value > 0


def _timestamp(raw: Any) -> bool:
    value = _integer(raw)
    return value is not None and value > 0


def _symbol(raw: Any) -> bool:
    return (
        isinstance(raw, str)
        and raw.endswith("_USDT")
        and len(raw) > 5
    )


def assess_private_ws_payload(
    channel: str,
    data: Any,
    *,
    envelope_ts: Any | None = None,
) -> dict[str, Any]:
    result: dict[str, Any] = {
        "channel": channel if channel in SUPPORTED_CHANNELS else "UNKNOWN",
        "document_contract_review_date": "2026-10-26",
        "payload_semantics_verified": False,
        "rest_correlation_verified": False,
        "lifecycle_semantics_verified": False,
        "live_authorization": False,
    }
    if channel not in SUPPORTED_CHANNELS or not isinstance(data, dict):
        result["status"] = "PRIVATE_WS_PAYLOAD_INVALID"
        return result
    if envelope_ts is not None and not _timestamp(envelope_ts):
        result["status"] = "PRIVATE_WS_ENVELOPE_TIMESTAMP_INVALID"
        return result

    if channel == POSITION_CHANNEL:
        required = (
            _positive_id(data.get("positionId")),
            _symbol(data.get("symbol")),
            _nonnegative_decimal(data.get("holdVol")),
            _integer(data.get("positionType")) in {1, 2},
            _integer(data.get("openType")) in {1, 2},
            _integer(data.get("state")) in {1, 2, 3},
        )
        if not all(required):
            result["status"] = "POSITION_WS_SEMANTICS_INVALID"
            return result
        for field in ("createTime", "updateTime"):
            if field in data and not _timestamp(data.get(field)):
                result["status"] = "POSITION_WS_TIMESTAMP_INVALID"
                return result
        if "leverage" in data:
            leverage = _integer(data.get("leverage"))
            if leverage is None or leverage < 0:
                result["status"] = "POSITION_WS_LEVERAGE_INVALID"
                return result
        result["payload_semantics_verified"] = True
        result["status"] = "POSITION_WS_PAYLOAD_SEMANTICS_VERIFIED"
        return result

    if channel == STOP_CHANNEL:
        required = (
            _positive_id(data.get("id")),
            _symbol(data.get("symbol")),
            _positive_id(data.get("positionId")),
            _integer(data.get("positionType")) in {1, 2},
            _integer(data.get("isFinished")) in {0, 1},
            _integer(data.get("volType")) in {1, 2},
            data.get("profitLossVolType") in {"SAME", "SEPARATE"},
            _integer(data.get("triggerSide")) in {0, 1, 2},
        )
        if not all(required):
            result["status"] = "STOP_WS_SEMANTICS_INVALID"
            return result

        state = _integer(data.get("state"))
        if state is None or state < 0:
            result["status"] = "STOP_WS_STATE_INVALID"
            return result
        stop_loss_price = _decimal(data.get("stopLossPrice"))
        if stop_loss_price is None or stop_loss_price < 0:
            result["status"] = "STOP_WS_PRICE_INVALID"
            return result
        for field in ("vol", "stopLossVol", "realityVol"):
            if field in data and not _nonnegative_decimal(data.get(field)):
                result["status"] = "STOP_WS_VOLUME_INVALID"
                return result
        for field in ("placeOrderId", "orderId"):
            if field in data:
                value = _integer(data.get(field))
                if value is None or value < 0:
                    result["status"] = "STOP_WS_LINKED_ORDER_ID_INVALID"
                    return result
        for field in ("createTime", "updateTime"):
            if field in data and not _timestamp(data.get(field)):
                result["status"] = "STOP_WS_TIMESTAMP_INVALID"
                return result

        # Entire-position SAME mode may legitimately omit/zero explicit vol.
        # SEPARATE mode must carry a positive stopLossVol when SL is present.
        if (
            data.get("profitLossVolType") == "SEPARATE"
            and stop_loss_price > 0
            and not _positive_decimal(data.get("stopLossVol"))
        ):
            result["status"] = "STOP_WS_SEPARATE_VOLUME_INVALID"
            return result

        result["payload_semantics_verified"] = True
        result["status"] = "STOP_WS_PAYLOAD_SEMANTICS_VERIFIED"
        return result

    required = (
        _positive_id(data.get("id")),
        _symbol(data.get("symbol")),
        _integer(data.get("side")) in {1, 2, 3, 4},
        _positive_decimal(data.get("vol")),
        _positive_decimal(data.get("price")),
        isinstance(data.get("feeCurrency"), str)
        and bool(data.get("feeCurrency")),
        _decimal(data.get("fee")) is not None,
        _timestamp(data.get("timestamp")),
        _integer(data.get("category")) is not None,
        _positive_id(data.get("orderId")),
        _integer(data.get("positionMode")) in {1, 2},
    )
    if not all(required):
        result["status"] = "FILL_WS_SEMANTICS_INVALID"
        return result
    if "isTaker" in data and type(data.get("isTaker")) is not bool:
        result["status"] = "FILL_WS_TAKER_FLAG_INVALID"
        return result
    if "reduceOnly" in data and type(data.get("reduceOnly")) is not bool:
        result["status"] = "FILL_WS_REDUCE_ONLY_INVALID"
        return result
    if "externalOid" in data and not isinstance(data.get("externalOid"), str):
        result["status"] = "FILL_WS_EXTERNAL_OID_INVALID"
        return result

    result["payload_semantics_verified"] = True
    result["status"] = "FILL_WS_PAYLOAD_SEMANTICS_VERIFIED"
    return result


def assess_private_ws_rest_correlation(
    channel: str,
    data: Any,
    rest_row: Any,
) -> dict[str, Any]:
    """Correlate one observed WS payload to one authoritative REST row."""
    result = assess_private_ws_payload(channel, data)
    result["rest_correlation_verified"] = False
    result["lifecycle_semantics_verified"] = False
    if result["payload_semantics_verified"] is not True:
        return result
    if not isinstance(rest_row, dict):
        result["status"] = "PRIVATE_WS_REST_ROW_INVALID"
        return result

    if channel == POSITION_CHANNEL:
        same = all((
            str(rest_row.get("positionId") or "")
            == str(data.get("positionId") or ""),
            rest_row.get("symbol") == data.get("symbol"),
            _integer(rest_row.get("positionType"))
            == _integer(data.get("positionType")),
            _integer(rest_row.get("state"))
            == _integer(data.get("state")),
            _decimal(rest_row.get("holdVol"))
            == _decimal(data.get("holdVol")),
        ))
    elif channel == STOP_CHANNEL:
        same = all((
            str(rest_row.get("id") or rest_row.get("stopPlanOrderId") or "")
            == str(data.get("id") or ""),
            str(rest_row.get("positionId") or "")
            == str(data.get("positionId") or ""),
            rest_row.get("symbol") == data.get("symbol"),
            _integer(rest_row.get("state")) == _integer(data.get("state")),
            _integer(rest_row.get("isFinished"))
            == _integer(data.get("isFinished")),
        ))
    else:
        same = all((
            str(rest_row.get("id") or "") == str(data.get("id") or ""),
            str(rest_row.get("orderId") or "")
            == str(data.get("orderId") or ""),
            rest_row.get("symbol") == data.get("symbol"),
            _integer(rest_row.get("side")) == _integer(data.get("side")),
            _decimal(rest_row.get("vol")) == _decimal(data.get("vol")),
            _decimal(rest_row.get("price")) == _decimal(data.get("price")),
        ))

    result["rest_correlation_verified"] = bool(same)
    result["lifecycle_semantics_verified"] = bool(same)
    result["status"] = (
        "PRIVATE_WS_REST_LIFECYCLE_SEMANTICS_VERIFIED"
        if same
        else "PRIVATE_WS_REST_CORRELATION_MISMATCH"
    )
    return result
