"""GET-only candidate open-stop provenance against MEXC account order counts.

This is a narrow CURRENT two-window witness, NOT an atomic snapshot or
complete account reconciliation. Uses only:
* private position/open_positions?symbol=...
* private order/open_order_total_count
* private stoporder/open_orders (all and symbol-scoped)

Two rounds; no POST, trade, cancel, stop amendment, leverage/margin changes,
database access, or Render. Private manual positions/orders stay untouched.
Only allowlisted counts and categories leave this module; never any symbol,
order ID, price, quantity, or signed request. Only the all-stop-only account
case may establish observed candidate absence for CURRENT TP/SL orders.
"""
from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from typing import Any

from app.t100_live_readonly_probe import ReadOnlyMexcProbe

_SYMBOL = re.compile(r"[A-Z0-9]+_USDT")
_STOP_FIELDS = (
    "id", "orderId", "positionId", "symbol", "positionType",
    "state", "isFinished", "vol", "stopLossPrice", "takeProfitPrice",
    "version", "updateTime", "stopLossVol", "takeProfitVol",
)
_POSITION_FIELDS = (
    "positionId", "id", "symbol", "positionType", "openType",
    "state", "holdVol", "frozenVol", "version", "updateTime",
)
_COUNT_KEYS = (
    "sumCount", "limitOrderCount", "stopOrderCount",
    "planOrderCount", "trackOrderCount",
)


@dataclass(frozen=True)
class CandidateStopScope:
    status: str
    candidate_positions_stable: bool = False
    candidate_open_positions: int | None = None
    account_order_counts_stable: bool = False
    total_open_orders: int | None = None
    limit_orders: int | None = None
    stop_orders: int | None = None
    plan_orders: int | None = None
    trailing_orders: int | None = None
    global_current_stops_stable: bool = False
    candidate_current_stops_stable: bool = False
    global_current_stops_observed: int | None = None
    candidate_current_stops_observed: int | None = None
    global_stop_count_agrees: bool = False
    symbol_scope_agrees_with_global: bool = False
    account_orders_all_tpsl_in_observed_window: bool = False
    candidate_current_tpsl_absent_observed: bool = False
    candidate_all_order_types_absent_certified: bool = False
    account_wide_completeness_certified: bool = False
    native_stop_protection_verified: bool = False
    entry_allowed: bool = False
    live_authorization: bool = False
    mexc_orders_sent: bool = False
    read_only: bool = True


def _digest(rows: Any, fields: tuple[str, ...],
            symbol: str | None = None) -> str:
    if type(rows) is not list or len(rows) > 1000:
        raise ValueError("COLLECTION_UNVERIFIED")
    seen: set[str] = set()
    canonical: list[dict[str, Any]] = []
    for row in rows:
        if type(row) is not dict:
            raise ValueError("ROW_UNVERIFIED")
        s = row.get("symbol")
        if (
            type(s) is not str or _SYMBOL.fullmatch(s) is None
            or (symbol is not None and s != symbol)
        ):
            raise ValueError("SYMBOL_UNVERIFIED")
        if fields == _STOP_FIELDS:
            key = row.get("id")
            if type(key) not in (int, str) or not str(key).isdigit() or int(key) <= 0:
                raise ValueError("STOP_ID_UNVERIFIED")
            if str(key) in seen:
                raise ValueError("STOP_ID_DUPLICATED")
            seen.add(str(key))
        canonical.append({field: row.get(field) for field in fields})
    # The exchange can reorder list rows without changing the observed state.
    canonical.sort(key=lambda row: json.dumps(
        row, sort_keys=True, separators=(",", ":"), default=str,
    ))
    blob = json.dumps(canonical, sort_keys=True, allow_nan=False,
                      separators=(",", ":"), default=str)
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


def _active_stops(rows: Any) -> bool:
    # Current open TP/SL should not contain finalized/cancelled orders.
    return all(
        type(row.get("state")) is int and row["state"] == 1
        and row.get("isFinished", 0) == 0
        for row in rows
    )


async def inspect_candidate_stop_scope(
    private: ReadOnlyMexcProbe, symbol: str,
) -> CandidateStopScope:
    def blocked(status: str, **kwargs: Any) -> CandidateStopScope:
        return CandidateStopScope(status=status, **kwargs)

    if type(symbol) is not str or _SYMBOL.fullmatch(symbol) is None:
        return blocked("CANDIDATE_STOP_SCOPE_SYMBOL_INVALID")
    if not isinstance(private, ReadOnlyMexcProbe):
        return blocked("CANDIDATE_STOP_SCOPE_CLIENT_INVALID")

    rounds: list[tuple[list[dict[str, Any]], dict[str, int],
                       list[dict[str, Any]], list[dict[str, Any]]]] = []
    for _ in range(2):
        try:
            positions = await private.raw_open_positions(symbol)
            counts = await private.raw_open_order_counts()
            global_stops = await private.raw_open_stops()
            scoped_stops = await private.raw_open_stops(symbol)
            # Validate schema before results leave this frame.
            _digest(positions, _POSITION_FIELDS, symbol)
            _digest(global_stops, _STOP_FIELDS)
            _digest(scoped_stops, _STOP_FIELDS, symbol)
            if not _active_stops(global_stops) or not _active_stops(scoped_stops):
                return blocked("CANDIDATE_STOP_SCOPE_NONACTIVE_ROWS")
            rounds.append((positions, counts, global_stops, scoped_stops))
        except Exception:
            return blocked("CANDIDATE_STOP_SCOPE_READ_OR_SCHEMA_FAILED")

    (p1, c1, g1, s1), (p2, c2, g2, s2) = rounds
    try:
        if _digest(p1, _POSITION_FIELDS, symbol) != _digest(
            p2, _POSITION_FIELDS, symbol
        ):
            return blocked("CANDIDATE_STOP_SCOPE_POSITION_DRIFT")
        if any(c1[key] != c2[key] for key in _COUNT_KEYS):
            return blocked("CANDIDATE_STOP_SCOPE_COUNTS_DRIFT")
        if _digest(g1, _STOP_FIELDS) != _digest(g2, _STOP_FIELDS):
            return blocked("CANDIDATE_STOP_SCOPE_GLOBAL_STOP_DRIFT")
        if _digest(s1, _STOP_FIELDS, symbol) != _digest(
            s2, _STOP_FIELDS, symbol
        ):
            return blocked("CANDIDATE_STOP_SCOPE_SYMBOL_STOP_DRIFT")
        filtered = [r for r in g2 if r.get("symbol") == symbol]
        # Two separately queried representations must refer to same records.
        if _digest(filtered, _STOP_FIELDS, symbol) != _digest(
            s2, _STOP_FIELDS, symbol
        ):
            return blocked("CANDIDATE_STOP_SCOPE_GLOBAL_SCOPED_MISMATCH")
    except Exception:
        return blocked("CANDIDATE_STOP_SCOPE_COMPARISON_FAILED")

    common = dict(
        candidate_positions_stable=True,
        candidate_open_positions=len(p2),
        account_order_counts_stable=True,
        total_open_orders=c2["sumCount"],
        limit_orders=c2["limitOrderCount"],
        stop_orders=c2["stopOrderCount"],
        plan_orders=c2["planOrderCount"],
        trailing_orders=c2["trackOrderCount"],
        global_current_stops_stable=True,
        candidate_current_stops_stable=True,
        global_current_stops_observed=len(g2),
        candidate_current_stops_observed=len(s2),
        global_stop_count_agrees=len(g2) == c2["stopOrderCount"],
        symbol_scope_agrees_with_global=True,
        account_orders_all_tpsl_in_observed_window=(
            c2["sumCount"] == c2["stopOrderCount"]
            and len(g2) == c2["stopOrderCount"]
        ),
        candidate_current_tpsl_absent_observed=len(s2) == 0,
    )
    if len(g2) != c2["stopOrderCount"]:
        return blocked("CANDIDATE_STOP_SCOPE_ACCOUNT_STOP_COUNT_MISMATCH",
                       **common)
    if p2:
        return blocked("CANDIDATE_STOP_SCOPE_EXISTING_POSITION_PRESENT", **common)
    if s2:
        return blocked("CANDIDATE_STOP_SCOPE_CANDIDATE_TPSL_PRESENT", **common)
    if c2["sumCount"] != c2["stopOrderCount"]:
        return blocked(
            "CANDIDATE_STOP_SCOPE_OTHER_ORDER_TYPES_UNSCOPED",
            **common,
        )
    return blocked(
        "CANDIDATE_STOP_SCOPE_NO_POSITION_NO_TPSL_OBSERVED_NONAUTHORIZING",
        **common,
    )
