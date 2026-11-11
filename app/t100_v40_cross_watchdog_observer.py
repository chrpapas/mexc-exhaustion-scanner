"""Independent V40 pilot GET-only FLAT or single-owned CROSS 1x observer.

Never a trade permit, stop writer, emergency-close executor, or substitute
for venue-native stop protection. The two modes are distinctly scoped:
FLAT can observe a dedicated empty account before any real entry; OWNED
needs externally reviewed immutable entry, position, quantity and stop IDs.
No automatic discovery, adoption, scope transition or ignored foreign rows.
A matching pair of signed GETs is a sampled observation, not continuous
protection or API pagination completeness certification.
"""
from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
import json
import re
import time
from typing import Any

from app.t100_live_native_stop_execution_audit import (
    assess_native_short_market_stop_row,
)
from app.t100_live_readonly_probe import ReadOnlyMexcProbe
from app.t100_pilot_watchdog_observer import PilotWatchVerdict


@dataclass(frozen=True)
class V40CrossWatchScope:
    account_key_sha256: str
    mode: str
    symbol: str | None = None
    position_id: str | None = None
    origin_oid: str | None = None
    entry_order_id: str | None = None
    stop_id: str | None = None
    contracts: Decimal | None = None
    max_short_stop_price: Decimal | None = None


def _id(value: Any) -> str | None:
    if type(value) not in (str,int):
        return None
    s=str(value)
    return s if re.fullmatch(r"[1-9][0-9]*",s) else None


def _num(value: Any) -> Decimal | None:
    if value is None or type(value) is bool:
        return None
    try:
        n=Decimal(str(value))
    except (ValueError,TypeError,InvalidOperation):
        return None
    return n if n.is_finite() and n>0 else None


def validate_v40_scope(scope: Any) -> bool:
    if (
        type(scope) is not V40CrossWatchScope
        or type(scope.account_key_sha256) is not str
        or re.fullmatch(r"[a-f0-9]{64}",scope.account_key_sha256) is None
        or scope.mode not in ("FLAT","OWNED")
    ):
        return False
    details=(
        scope.symbol,scope.position_id,scope.origin_oid,
        scope.entry_order_id,scope.stop_id,
        scope.contracts,scope.max_short_stop_price,
    )
    if scope.mode=="FLAT":
        return all(value is None for value in details)
    return bool(
        type(scope.symbol) is str
        and re.fullmatch(r"[A-Z0-9]+_USDT",scope.symbol)
        and scope.symbol not in ("BTC_USDT","ETH_USDT")
        and _id(scope.position_id) is not None
        and type(scope.origin_oid) is str
        and re.fullmatch(r"t100[a-zA-Z0-9_]{8,60}",scope.origin_oid)
        and _id(scope.entry_order_id) is not None
        and _id(scope.stop_id) is not None
        and type(scope.contracts) is Decimal
        and _num(scope.contracts) is not None
        and type(scope.max_short_stop_price) is Decimal
        and _num(scope.max_short_stop_price) is not None
    )


_POSITION_FIELDS=(
    "symbol","positionId","state","positionType","holdVol",
    "openType","leverage","freezeVol",
)
_STOP_FIELDS=(
    "symbol","positionId","id","stopPlanOrderId","state","isFinished",
    "positionType","stopLossType","stopLossPrice","volType",
    "profitLossVolType","stopLossReverse","lossTrend","triggerSide",
    "errorCode","orderId","vol","stopLossVol",
)
_COUNTS=(
    "sumCount","limitOrderCount","stopOrderCount",
    "planOrderCount","trackOrderCount",
)


def _snapshot_fingerprint(snapshot: dict[str,Any]) -> str | None:
    p,s,c,m=(snapshot.get(k) for k in ("positions","stops","counts","mode"))
    if (
        type(p) is not list or type(s) is not list
        or any(type(x) is not dict for x in p+s)
        or type(c) is not dict
        or any(type(c.get(k)) is not int or c[k]<0 for k in _COUNTS)
        or c["sumCount"]!=sum(c[k] for k in _COUNTS[1:])
        or c["stopOrderCount"]!=len(s)
        or type(m) is not int or m!=1
    ):
        return None
    # Account-wide results may have arbitrary list order. Preserve all
    # contract/stop safety fields and duplicates; never use PnL fluctuations.
    try:
        def canonical(rows,fields):
            return sorted(json.dumps(
                {k:r[k] for k in fields if k in r},
                sort_keys=True,separators=(",",":"),allow_nan=False,
            ) for r in rows)
        return json.dumps({
            "p":canonical(p,_POSITION_FIELDS),
            "s":canonical(s,_STOP_FIELDS),
            "c":{k:c[k] for k in _COUNTS},"m":m,
        },sort_keys=True,separators=(",",":"),allow_nan=False)
    except (ValueError,TypeError):
        return None


def _alert(status: str) -> PilotWatchVerdict:
    return PilotWatchVerdict(status=status)


def _check_origin_and_deals(
    scope: V40CrossWatchScope,order: Any,deals: Any,
) -> bool:
    if (
        type(order) is not dict
        or order.get("symbol")!=scope.symbol
        or order.get("externalOid")!=scope.origin_oid
        or _id(order.get("orderId"))!=scope.entry_order_id
        or _id(order.get("positionId"))!=scope.position_id
        or type(order.get("state")) is not int or order["state"]!=3
        or type(order.get("side")) is not int or order["side"]!=3
        or _num(order.get("dealVol"))!=scope.contracts
        or type(deals) is not list or not 1<=len(deals)<=100
    ):
        return False
    ids=set()
    total=Decimal(0)
    for deal in deals:
        if type(deal) is not dict or _id(deal.get("id")) is None:
            return False
        did=_id(deal["id"])
        if (
            did in ids or _id(deal.get("orderId"))!=scope.entry_order_id
            or deal.get("symbol")!=scope.symbol
            or type(deal.get("side")) is not int or deal["side"]!=3
            or _num(deal.get("price")) is None
            or _num(deal.get("vol")) is None
        ):
            return False
        ids.add(did)
        total+=_num(deal["vol"])
    return total==scope.contracts


def assess_v40_cross_watch(
    scope: V40CrossWatchScope,
    first: Any,second: Any,
    *,order: Any=None,deals: Any=None,
) -> PilotWatchVerdict:
    """Pure, fail-closed independent snapshot check. No I/O or mutations."""
    if not validate_v40_scope(scope):
        return _alert("WATCH_V40_SCOPE_INVALID")
    if type(first) is not dict or type(second) is not dict:
        return _alert("WATCH_V40_SIGNED_INVENTORY_INVALID")
    fp1=_snapshot_fingerprint(first)
    fp2=_snapshot_fingerprint(second)
    if fp1 is None or fp2 is None:
        return _alert("WATCH_V40_INVENTORY_OR_HEDGE_MODE_UNVERIFIED")
    if fp1!=fp2:
        return _alert("WATCH_V40_CRITICAL_BOOKEND_DRIFT")
    positions=second["positions"]
    stops=second["stops"]
    counts=second["counts"]
    if scope.mode=="FLAT":
        if any((positions,stops,counts["sumCount"])):
            return _alert("WATCH_V40_FLAT_SCOPE_UNEXPECTED_EXPOSURE")
        return PilotWatchVerdict(
            status="WATCH_FLAT_INVENTORY_OBSERVED_NONAUTHORIZING",
            alert_supervisor=False,account_flat_observed=True,
        )
    # One specifically owned CROSS short only; never adopt manual or
    # ambiguous positions, stop replacements, other active orders or slots.
    if (
        len(positions)!=1 or len(stops)!=1
        or counts["sumCount"]!=1 or counts["stopOrderCount"]!=1
    ):
        return _alert("WATCH_V40_OWNED_OR_ORDER_COUNT_MISMATCH")
    pos=positions[0]
    stop=stops[0]
    if (
        pos.get("symbol")!=scope.symbol
        or _id(pos.get("positionId"))!=scope.position_id
        or type(pos.get("state")) is not int or pos["state"]!=1
        or type(pos.get("positionType")) is not int or pos["positionType"]!=2
        or type(pos.get("openType")) is not int or pos["openType"]!=2
        or type(pos.get("leverage")) is not int or pos["leverage"]!=1
        or _num(pos.get("holdVol"))!=scope.contracts
    ):
        return _alert("WATCH_V40_OWNED_CROSS_POSITION_UNVERIFIED")
    if not _check_origin_and_deals(scope,order,deals):
        return _alert("WATCH_V40_DURABLE_ORIGIN_AND_FILLS_UNVERIFIED")
    if _id(stop.get("id") or stop.get("stopPlanOrderId"))!=scope.stop_id:
        return _alert("WATCH_V40_NATIVE_STOP_IDENTITY_CHANGED")
    audit=assess_native_short_market_stop_row(
        stop=stop,symbol=scope.symbol,position_id=scope.position_id,
        position_quantity=scope.contracts,
        maximum_short_stop_price=scope.max_short_stop_price,
        verified_filled_entry_order_id=scope.entry_order_id,
    )
    if audit.active_native_full_volume_market_sl_observed is not True:
        return _alert("WATCH_V40_NATIVE_MARKET_STOP_NOT_VERIFIED")
    return PilotWatchVerdict(
        status="WATCH_NATIVE_STOP_CURRENTLY_OBSERVED_NONAUTHORIZING",
        alert_supervisor=False,currently_active_stop_observed=True,
    )


async def observe_v40_cross_get(
    probe: ReadOnlyMexcProbe,scope: V40CrossWatchScope,
) -> PilotWatchVerdict:
    """One bounded two-bookend GET observation, never a continuous guarantee."""
    if type(probe) is not ReadOnlyMexcProbe or not validate_v40_scope(scope):
        return _alert("WATCH_V40_ADAPTER_OR_SCOPE_INVALID")
    async def snapshot() -> dict[str,Any]:
        return {
            "positions":await probe.raw_open_positions(),
            "stops":await probe.raw_open_stops(),
            "counts":await probe.raw_open_order_counts(),
            "mode":await probe.raw_position_mode(),
        }
    start=time.monotonic()
    try:
        first=await snapshot()
        origin=None
        deals=None
        if scope.mode=="OWNED":
            origin=await probe.raw_order_by_external_oid(
                scope.symbol,scope.origin_oid,
            )
            deals=await probe.raw_order_deal_details(scope.entry_order_id)
        second=await snapshot()
    except Exception:
        return _alert("WATCH_V40_SIGNED_GET_UNAVAILABLE")
    if time.monotonic()-start>8:
        return _alert("WATCH_V40_SIGNED_GET_CYCLE_TOO_SLOW")
    return assess_v40_cross_watch(
        scope,first,second,order=origin,deals=deals,
    )
