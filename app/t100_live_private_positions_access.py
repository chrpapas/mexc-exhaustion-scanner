"""Single-request, read-only MEXC futures private-positions access diagnosis.

Use only with operator-provided paper-mode / kill-switch controls.
This module never performs POST, order creation, margin changes, WebSocket
writes, database writes, or automatic retries.

All output is from an allowlist. Never print raw response bodies, URLs,
headers, credentials, symbols, quantities, balances, or exception messages.

Official MEXC futures codes (access/auth subset) are mapped explicitly.
Other venue error codes are classified as UNKNOWN, never echoed.
"""
from __future__ import annotations

import asyncio
from typing import Any, Callable, Mapping

OPEN_POSITIONS_PATH = "/api/v1/private/position/open_positions"

# Public MEXC futures API error-code documentation (2026-10-29).
# No untrusted exchange error code or message is returned verbatim.
_SAFE_MEXC_ERRORS: dict[int, str] = {
    401: "AUTHENTICATION_REJECTED",
    402: "API_KEY_EXPIRED",
    406: "IP_NOT_WHITELISTED",
    500: "EXCHANGE_INTERNAL_ERROR",
    501: "EXCHANGE_BUSY",
    506: "UNKNOWN_REQUEST_SOURCE",
    510: "RATE_LIMITED",
    511: "ENDPOINT_PERMISSION_DENIED",
    513: "REQUEST_OR_TIMESTAMP_INVALID",
    600: "REQUEST_PARAMETERS_INVALID",
    601: "REQUEST_DATA_INVALID",
    602: "SIGNATURE_REJECTED",
    603: "DUPLICATE_REQUEST_REJECTED",
    604: "EXCHANGE_MAINTENANCE",
    701: "ACCOUNT_READ_PERMISSION_REQUIRED",
    703: "TRADING_INFORMATION_READ_PERMISSION_REQUIRED",
    801: "EXCHANGE_MAINTENANCE",
    1000: "FUTURES_ACCOUNT_UNAVAILABLE",
    9999: "EXCHANGE_NETWORK_ERROR",
}


def _report(status: str, *, http_status: int | None = None) -> dict[str, Any]:
    """Fixed-category reporting only; never emit exchange fields."""
    allowed = set(_SAFE_MEXC_ERRORS.values()) | {
        "POSITIONS_READ_SUCCEEDED",
        "HTTP_AUTHENTICATION_REJECTED",
        "HTTP_RATE_LIMITED",
        "HTTP_SERVER_ERROR",
        "HTTP_CLIENT_ERROR",
        "NETWORK_TIMEOUT",
        "NETWORK_ERROR",
        "AUTH_HEADERS_UNAVAILABLE",
        "MALFORMED_EXCHANGE_RESPONSE",
        "UNCLASSIFIED_EXCHANGE_ERROR",
        "DIAGNOSTIC_INPUT_INVALID",
        "DIAGNOSTIC_RUNTIME_FAILED",
        "DIAGNOSTIC_CREDENTIALS_MISSING",
        "DIAGNOSTIC_PAPER_KILL_REQUIRED",
    }
    safe_status = status if status in allowed else "UNCLASSIFIED_EXCHANGE_ERROR"
    allowed_http_status = {200, 400, 401, 403, 404, 408, 429, 500, 502, 503, 504}
    return {
        "status": safe_status,
        "http_status": (
            http_status if type(http_status) is int
            and http_status in allowed_http_status else None
        ),
        "read_only": True,
        "private_get_only": True,
        "one_request_maximum": True,
        "mexc_orders_sent": False,
        "entry_allowed": False,
        "live_authorization": False,
        "account_wide_completeness_certified": False,
        "venue_liquidation_certified": False,
    }


def _classify_response(status_code: Any, payload: Any) -> dict[str, Any]:
    if type(status_code) is not int or status_code < 100 or status_code > 599:
        return _report("MALFORMED_EXCHANGE_RESPONSE")
    safe_http = status_code
    if isinstance(payload, dict) and payload.get("success") is True:
        if (
            status_code == 200
            and type(payload.get("code")) is int
            and payload["code"] == 0
            and isinstance(payload.get("data"), list)
            and all(type(row) is dict for row in payload["data"])
        ):
            return _report("POSITIONS_READ_SUCCEEDED", http_status=safe_http)
        return _report("MALFORMED_EXCHANGE_RESPONSE", http_status=safe_http)

    if isinstance(payload, dict) and payload.get("success") is False:
        code = payload.get("code")
        if type(code) is int and code in _SAFE_MEXC_ERRORS:
            return _report(_SAFE_MEXC_ERRORS[code], http_status=safe_http)

    if status_code in (401, 403):
        return _report("HTTP_AUTHENTICATION_REJECTED", http_status=safe_http)
    if status_code == 429:
        return _report("HTTP_RATE_LIMITED", http_status=safe_http)
    if 500 <= status_code <= 599:
        return _report("HTTP_SERVER_ERROR", http_status=safe_http)
    if 400 <= status_code <= 499:
        return _report("HTTP_CLIENT_ERROR", http_status=safe_http)
    return _report("UNCLASSIFIED_EXCHANGE_ERROR", http_status=safe_http)


async def probe_private_positions_access(
    *,
    http_client: Any,
    signed_headers: Callable[[str, str], Mapping[str, str]],
    clock_ms: Callable[[], int],
) -> dict[str, Any]:
    """Exactly one authenticated GET, no automatic retry and no raw logging."""
    if http_client is None or not callable(signed_headers) or not callable(clock_ms):
        return _report("DIAGNOSTIC_INPUT_INVALID")

    try:
        ts = clock_ms()
        if type(ts) is not int or ts <= 0:
            return _report("DIAGNOSTIC_INPUT_INVALID")
        headers = signed_headers(str(ts), "")
        if not isinstance(headers, Mapping):
            return _report("AUTH_HEADERS_UNAVAILABLE")
    except Exception:
        return _report("AUTH_HEADERS_UNAVAILABLE")

    try:
        response = await http_client.get(OPEN_POSITIONS_PATH, headers=headers)
    except (asyncio.TimeoutError, TimeoutError):
        return _report("NETWORK_TIMEOUT")
    except Exception as exc:
        # Don't inspect or print exception str, repr, request URL or headers.
        # These may include sensitive HTTP metadata.
        if type(exc).__name__ in {
            "ConnectTimeout", "ReadTimeout", "WriteTimeout", "PoolTimeout"
        }:
            return _report("NETWORK_TIMEOUT")
        return _report("NETWORK_ERROR")

    status = getattr(response, "status_code", None)
    try:
        payload = response.json()
    except Exception:
        payload = None
    return _classify_response(status, payload)
