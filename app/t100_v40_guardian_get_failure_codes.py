"""Fixed-category diagnostics for the V40 guardian position-mode signed GET.

No credentials, raw URLs, HTTP bodies, response message strings, or free-form
exception details may enter a code. Diagnostic strings NEVER mean safe account
inventory or grant a trade permit. The original sticky latch remains intact.
"""
from __future__ import annotations


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
    """Status/exception *type* only; never print or preserve exception text.

    Import httpx only on actual network failures, so pure policy and snapshot
    modules remain dependency-free in their offline safety test environment.
    """
    import httpx
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


# Optional, opt-in forensic categories for the guardian's other signed GETs.
# Values are finite constants. Never log endpoint URLs, request/response bodies,
# headers, free-form exception strings, SQL values or MEXC account identifiers.
_GENERIC_GET_SUFFIXES=frozenset({
    "HTTP_401","HTTP_403","HTTP_429","HTTP_5XX","HTTP_OTHER",
    "TIMEOUT","NETWORK","TRANSPORT_OTHER",
    "API_RATE_LIMIT","API_AUTH","API_PERMISSION","API_SERVER","API_REJECTION",
})
SAFE_INVENTORY_GET_REASONS=frozenset(
    "GUARDIAN_GET_"+reason for reason in _GENERIC_GET_SUFFIXES
)


def classify_inventory_get_transport_failure(error: Exception) -> str:
    reason=classify_mode_transport_failure(error).removeprefix("POSITION_MODE_")
    return "GUARDIAN_GET_"+reason if reason in _GENERIC_GET_SUFFIXES else (
        "GUARDIAN_GET_TRANSPORT_OTHER"
    )


def classify_inventory_get_api_rejection(payload: object) -> str:
    reason=classify_mode_api_rejection(payload).removeprefix("POSITION_MODE_")
    return "GUARDIAN_GET_"+reason if reason in _GENERIC_GET_SUFFIXES else (
        "GUARDIAN_GET_API_REJECTION"
    )


def safe_inventory_exception_category(error: BaseException) -> str:
    """Look through at most two *internal* typed exception wrappers.

    Never serialize an exception, cause, body, request, URL, or user data.
    A response-format fault is distinct from a typed exchange rejection.
    """
    from app.t100_live_guardian_readonly_once import GuardianOnceHalted
    from app.t100_live_readonly_probe import VenueReadHalted
    item=error
    for _ in range(3):
        if type(item) is GuardianOnceHalted and len(item.args)==1:
            code=item.args[0]
            if type(code) is str and code in SAFE_INVENTORY_GET_REASONS:
                return code.removeprefix("GUARDIAN_GET_")
        if item is None:
            break
        item=item.__cause__
    if type(error) is VenueReadHalted and len(error.args)==1:
        if error.args[0] in (
            "POSITIONS_RESPONSE_UNVERIFIED",
            "OPEN_STOPS_RESPONSE_UNVERIFIED",
            "ACCOUNT_ASSETS_RESPONSE_UNVERIFIED",
            "OPEN_ORDER_COUNTS_RESPONSE_UNVERIFIED",
        ):
            return "SCHEMA"
    return "UNCLASSIFIED"


_INVENTORY_STAGES=frozenset({
    "FIRST_POSITIONS","FIRST_STOPS","FIRST_DIGEST","COMPLETENESS",
    "AFTER_POSITIONS","AFTER_STOPS","AFTER_DIGEST",
    "BEFORE_POSITIONS","BEFORE_STOPS","SYMBOL_CROSSCHECK",
    "ACCOUNT_ASSETS","POSITION_MARGIN","OPEN_ORDER_COUNTS","CERTIFICATE",
})
_INVENTORY_CATEGORIES=_GENERIC_GET_SUFFIXES | {"SCHEMA","UNCLASSIFIED"}
SAFE_GUARDIAN_INVENTORY_DIAGNOSTIC_HALTS=frozenset(
    "V40_GUARDIAN_INVENTORY_"+stage+"_"+category+"_HALT"
    for stage in _INVENTORY_STAGES
    for category in _INVENTORY_CATEGORIES
)


def guardian_inventory_diagnostic_halt(stage: object, category: object) -> str | None:
    if (type(stage) is not str or stage not in _INVENTORY_STAGES
        or type(category) is not str or category not in _INVENTORY_CATEGORIES):
        return None
    return "V40_GUARDIAN_INVENTORY_"+stage+"_"+category+"_HALT"
