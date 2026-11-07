"""Independent GET-only pilot exposure observer (NOT an emergency-close executor).

Deliberately separate from scanner/trader, DB, Render and order transport.
Derives owned position from a precommitted external order identity; never
adopts a manual position. Every conclusion is a bounded, non-atomic
observation, not a certification of continuous protection or safe liquidation.

A real independent watchdog still requires a separate running service,
durable alarm delivery, supervised close transport, recovery drill and review.
This module has no POST, credentials, network client or dispatch mechanism.
"""
from __future__ import annotations

import re
import time
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from typing import Any

from app.t100_live_native_stop_execution_audit import assess_native_short_market_stop_row
from app.t100_live_readonly_probe import ReadOnlyMexcProbe

_SYMBOL = "BTC_USDT"


@dataclass(frozen=True)
class PilotWatchScope:
    account: str                  # SHA-256 account fingerprint, never a credential
    origin_oid: str               # durably committed pilot entry identity
    stop_id: str | None           # only the reconciled owned native-stop ID
    maximum_stop_price: Decimal  # operator-reviewed upper bound, not loss cap
    alert_after_ms: int           # alert threshold only; NOT permission to enter


@dataclass(frozen=True)
class PilotWatchVerdict:
    status: str
    alert_supervisor: bool = True
    currently_active_stop_observed: bool = False
    account_flat_observed: bool = False
    entry_allowed: bool = False
    live_authorization: bool = False
    stop_execution_certified: bool = False
    continuous_protection_certified: bool = False
    emergency_close_executed: bool = False
    mexc_orders_sent: bool = False
    read_only: bool = True


def _halt(code: str) -> PilotWatchVerdict:
    return PilotWatchVerdict(status=code)


def _positive(raw: Any) -> Decimal | None:
    if type(raw) is bool:
        return None
    try:
        value = Decimal(str(raw))
    except (ValueError, TypeError, InvalidOperation):
        return None
    return value if value.is_finite() and value > 0 else None


def _id(raw: Any) -> str | None:
    if type(raw) not in (str, int):
        return None
    val = str(raw)
    return val if re.fullmatch(r"[1-9][0-9]*", val) else None


def _scope_valid(scope: Any) -> bool:
    return (
        type(scope) is PilotWatchScope
        and type(scope.account) is str
        and re.fullmatch(r"[a-f0-9]{64}", scope.account) is not None
        and type(scope.origin_oid) is str
        and re.fullmatch(r"[A-Za-z0-9_-]{1,32}", scope.origin_oid) is not None
        and (scope.stop_id is None or _id(scope.stop_id) is not None)
        and type(scope.maximum_stop_price) is Decimal
        and _positive(scope.maximum_stop_price) is not None
        and type(scope.alert_after_ms) is int
        and 1 <= scope.alert_after_ms <= 15000
    )


def assess_pilot_watch(
    scope: PilotWatchScope, evidence: Any, *, now_ms: int,
) -> PilotWatchVerdict:
    """Pure fail-closed policy; incoming snapshots must come from trusted GET adapter.

    Expects a double-read inventory and double-read entry/fill ledger. Identical
    bookends are NOT atomic, authenticated by this function, or continuously
    representative of the state between reads.
    """
    if not _scope_valid(scope) or type(now_ms) is not int or now_ms < 1_500_000_000_000:
        return _halt("WATCH_SCOPE_OR_CLOCK_INVALID")
    if type(evidence) is not dict or evidence.get("account") != scope.account:
        return _halt("WATCH_ACCOUNT_OR_EVIDENCE_UNVERIFIED")
    start, end = evidence.get("start_ms"), evidence.get("end_ms")
    if (
        type(start) is not int or type(end) is not int
        or start > end or end > now_ms + 2000
        or now_ms - end > 3000 or end - start > 4000
    ):
        return _halt("WATCH_SIGNED_GET_WINDOW_STALE_OR_INVALID")
    before, after = evidence.get("before"), evidence.get("after")
    origin, origin2 = evidence.get("origin"), evidence.get("origin_bookend")
    deals, deals2 = evidence.get("deals"), evidence.get("deals_bookend")
    if type(before) is not dict or type(after) is not dict:
        return _halt("WATCH_INVENTORY_UNVERIFIED")
    if before != after or origin != origin2 or deals != deals2:
        return _halt("WATCH_BOOKEND_DRIFT_UNVERIFIED")
    positions, stops, counts = (before.get(k) for k in ("positions", "stops", "counts"))
    if (
        type(positions) is not list or type(stops) is not list or type(counts) is not dict
        or any(type(r) is not dict for r in positions + stops)
        or any(type(counts.get(k)) is not int or counts[k] < 0
               for k in ("sumCount", "stopOrderCount", "limitOrderCount",
                         "planOrderCount", "trackOrderCount"))
    ):
        return _halt("WATCH_INVENTORY_SHAPE_UNVERIFIED")
    if (
        counts["sumCount"] != sum(counts[k] for k in (
            "stopOrderCount", "limitOrderCount", "planOrderCount", "trackOrderCount"
        ))
        or counts["stopOrderCount"] != len(stops)
        or counts["sumCount"] != len(stops)
    ):
        return _halt("WATCH_UNRELATED_ORDER_OR_COUNT_MISMATCH")
    if (
        type(origin) is not dict
        or origin.get("symbol") != _SYMBOL
        or origin.get("externalOid") != scope.origin_oid
        or origin.get("state") != 3
        or origin.get("side") != 3
        or _id(origin.get("orderId")) is None
        or _id(origin.get("positionId")) is None
        or _positive(origin.get("dealVol")) != Decimal(1)
    ):
        return _halt("WATCH_OWNED_ENTRY_UNVERIFIED")
    if type(deals) is not list or not 1 <= len(deals) <= 100:
        return _halt("WATCH_ENTRY_FILL_LEDGER_UNVERIFIED")
    first_fill = now_ms
    vol = Decimal(0)
    seen_deals: set[str] = set()
    for d in deals:
        if type(d) is not dict or _id(d.get("id")) is None:
            return _halt("WATCH_ENTRY_FILL_LEDGER_UNVERIFIED")
        trade_id = _id(d["id"])
        ts, q = d.get("timestamp"), _positive(d.get("vol"))
        if (
            trade_id in seen_deals
            or _id(d.get("orderId")) != _id(origin["orderId"])
            or d.get("symbol") != _SYMBOL or d.get("side") != 3
            or q is None or _positive(d.get("price")) is None
            or type(ts) is not int
            or ts < 1_500_000_000_000 or ts > now_ms + 2000
        ):
            return _halt("WATCH_ENTRY_FILL_LEDGER_UNVERIFIED")
        seen_deals.add(trade_id)
        vol += q
        first_fill = min(first_fill, ts)
    if vol != Decimal(1):
        return _halt("WATCH_ENTRY_FILL_VOLUME_UNVERIFIED")

    if not positions and not stops and counts["sumCount"] == 0:
        # Does not distinguish ordinary close from triggered stop or liquidation.
        return PilotWatchVerdict(
            status="WATCH_FLAT_INVENTORY_OBSERVED_NONAUTHORIZING",
            alert_supervisor=False, account_flat_observed=True,
        )
    if (
        len(positions) != 1
        or _id(positions[0].get("positionId")) != _id(origin["positionId"])
        or positions[0].get("symbol") != _SYMBOL
        or _positive(positions[0].get("holdVol")) != Decimal(1)
        or type(positions[0].get("positionType")) is not int
        or positions[0]["positionType"] != 2
        or type(positions[0].get("openType")) is not int
        or positions[0]["openType"] != 1
        or type(positions[0].get("leverage")) is not int
        or positions[0]["leverage"] != 1
    ):
        return _halt("WATCH_POSITION_OWNERSHIP_OR_MODE_UNVERIFIED")

    if not stops:
        if now_ms - first_fill > scope.alert_after_ms:
            return _halt("WATCH_UNPROTECTED_EXPOSURE_DEADLINE_BREACHED")
        return _halt("WATCH_UNPROTECTED_EXPOSURE_NOW")
    if len(stops) != 1 or scope.stop_id is None or _id(stops[0].get("id")) != scope.stop_id:
        return _halt("WATCH_STOP_IDENTITY_OR_UNIQUENESS_UNVERIFIED")
    s = stops[0]
    if type(s.get("volType")) is not int or s["volType"] != 2 or s.get("profitLossVolType") != "SAME":
        return _halt("WATCH_STOP_WHOLE_POSITION_MODE_UNVERIFIED")
    # An entire-position stop can report zero/omitted volume; positive
    # reported volume MUST equal the one-contract account position.
    reported = s.get("vol")
    if reported is not None:
        if type(reported) is bool:
            return _halt("WATCH_STOP_VOLUME_CONFLICT")
        try:
            rvol = Decimal(str(reported))
        except (ValueError, TypeError, InvalidOperation):
            return _halt("WATCH_STOP_VOLUME_CONFLICT")
        if not rvol.is_finite() or rvol < 0 or (rvol > 0 and rvol != Decimal(1)):
            return _halt("WATCH_STOP_VOLUME_CONFLICT")
    native = assess_native_short_market_stop_row(
        stop=s, symbol=_SYMBOL, position_id=_id(origin["positionId"]),
        position_quantity=Decimal(1),
        maximum_short_stop_price=scope.maximum_stop_price,
    )
    if not native.active_native_full_volume_market_sl_observed:
        return _halt("WATCH_NATIVE_STOP_SEMANTICS_UNVERIFIED")
    return PilotWatchVerdict(
        status="WATCH_NATIVE_STOP_CURRENTLY_OBSERVED_NONAUTHORIZING",
        alert_supervisor=False, currently_active_stop_observed=True,
    )


async def observe_pilot_watch_get(
    probe: ReadOnlyMexcProbe, scope: PilotWatchScope,
) -> PilotWatchVerdict:
    """One GET-only observation, not a running independently deployed watchdog.

    No raw errors, venue identities or credentials are returned or persisted.
    A failed GET is UNKNOWN and never evidence of a flat account.
    """
    if type(probe) is not ReadOnlyMexcProbe or not _scope_valid(scope):
        return _halt("WATCH_GET_ADAPTER_OR_SCOPE_INVALID")
    start = int(time.time() * 1000)

    async def inventory():
        return {
            "positions": await probe.raw_open_positions(),
            "stops": await probe.raw_open_stops(),
            "counts": await probe.raw_open_order_counts(),
        }
    try:
        before = await inventory()
        origin = await probe.raw_order_by_external_oid(_SYMBOL, scope.origin_oid)
        if type(origin) is not dict or _id(origin.get("orderId")) is None:
            return _halt("WATCH_ENTRY_ORIGIN_GET_UNVERIFIED")
        deals = await probe.raw_order_deal_details(_id(origin["orderId"]))
        after = await inventory()
        origin2 = await probe.raw_order_by_external_oid(_SYMBOL, scope.origin_oid)
        deals2 = await probe.raw_order_deal_details(_id(origin["orderId"]))
        end = int(time.time() * 1000)
        return assess_pilot_watch(scope, {
            "account": scope.account,
            "start_ms": start, "end_ms": end,
            "before": before, "after": after,
            "origin": origin, "origin_bookend": origin2,
            "deals": deals, "deals_bookend": deals2,
        }, now_ms=end)
    except Exception:
        return _halt("WATCH_SIGNED_GET_UNAVAILABLE")
