"""Fixed-category diagnostics for the V40 guardian position-mode signed GET.

No credentials, raw URLs, HTTP bodies, response message strings, or free-form
exception details may enter a code. Diagnostic strings NEVER mean safe account
inventory or grant a trade permit. The original sticky latch remains intact.
"""
from __future__ import annotations

import httpx

MODE_PATH = "/api/v1/private/position/position_mode"

_CODES = frozenset({
    "POSITION_MODE_HTTP_401",
    "POSITION_MODE_HTTP_403",
    "POSITION_MODE_HTTP_429",
    "POSITION_MODE_HTTP_5XX",
    "POSITION_MODE_HTTP_OTHER",
    "POSITION_MODE_TIMEOUT",
    "POSITION_MODE_NETWORK",
    "POSITION_MODE_TRANSPORT_OTHER",
    "POSITION_MODE_API_RATE_LIMIT",
    "POSITION_MODE_API_AUTH",
    "POSITION_MODE_API_PERMISSION",
    "POSITION_MODE_API_SERVER",
    "POSITION_MODE_API_REJECTION",
})


def classify_mode_transport_failure(error: Exception) -> str:
    """Status/exception *type* only; never print or preserve exception text."""
    if isinstance(error, httpx.TimeoutException):
        return "POSITION_MODE_TIMEOUT"
    if isinstance(error, httpx.HTTPStatusError):
        status = error.response.status_code
        if status == 401:
            return "POSITION_MODE_HTTP_401"
        if status == 403:
            return "POSITION_MODE_HTTP_403"
        if status == 429:
            return "POSITION_MODE_HTTP_429"
        if 500 <= status <= 599:
            return "POSITION_MODE_HTTP_5XX"
        return "POSITION_MODE_HTTP_OTHER"
    if isinstance(error, httpx.TransportError):
        return "POSITION_MODE_NETWORK"
    return "POSITION_MODE_TRANSPORT_OTHER"


def classify_mode_api_rejection(payload: object) -> str:
    """Recognize ONLY documented fixed numeric codes, never response body text."""
    code = payload.get("code") if type(payload) is dict else None
    if type(code) is int:
        if code in (510,):
            return "POSITION_MODE_API_RATE_LIMIT"
        if code in (401, 402, 602):
            return "POSITION_MODE_API_AUTH"
        if code in (406, 511, 701, 703):
            return "POSITION_MODE_API_PERMISSION"
        if code in (500, 501, 604, 801):
            return "POSITION_MODE_API_SERVER"
    return "POSITION_MODE_API_REJECTION"


def safe_wrapped_mode_reason(error: Exception) -> str | None:
    """Only our fixed GuardianOnceHalted reason may cross the read-only probe."""
    if type(error).__name__ != "GuardianOnceHalted":
        return None
    value = error.args[0] if len(error.args) == 1 else None
    return value if type(value) is str and value in _CODES else None


def guardian_mode_halt_from_venue_error(error: Exception) -> str:
    """The returned status always halts, alerts, and preserves sticky incidents."""
    reason = error.args[0] if len(error.args) == 1 else None
    if type(reason) is str and reason in _CODES:
        return "V40_GUARDIAN_HEDGE_" + reason + "_HALT"
    return "V40_GUARDIAN_HEDGE_MODE_SIGNED_GET_FAILED_HALT"


SAFE_GUARDIAN_MODE_HALTS = frozenset(
    "V40_GUARDIAN_HEDGE_" + code + "_HALT" for code in _CODES
)
