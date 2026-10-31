"""Nonauthorizing read-only MEXC candidate position/order conflict screen.

Two symbol-scoped open-position GETs and two account-wide order-count GETs.
Unrelated manual positions are untouched. No stop or order placement/cancel,
leverage/margin changes, credentials in output, DB/Render, or risk permission.
Unscoped open-order counts cannot prove a candidate symbol has no orders.
No venue atomicity, account-wide completeness, liquidation, or cross-mode
certification. Symbols and raw position/order values never appear in output.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Any

from app.t100_live_readonly_probe import ReadOnlyMexcProbe

_SYMBOL = re.compile(r"[A-Z0-9]+_USDT")
_POSITION_KEYS = (
    "symbol", "positionId", "id", "positionType", "openType",
    "state", "holdVol", "frozenVol", "leverage",
)
_COUNT_KEYS = (
    "sumCount", "limitOrderCount", "stopOrderCount",
    "planOrderCount", "trackOrderCount",
)


@dataclass(frozen=True)
class CandidatePositionConflict:
    status: str
    symbol_scoped_positions_two_get_consistent: bool = False
    candidate_open_positions_observed: int | None = None
    candidate_short_position_observed: bool = False
    candidate_short_mode: str = "NOT_CHECKED"
    candidate_no_open_positions_observed: bool = False
    account_order_counts_two_get_consistent: bool = False
    account_open_orders_observed: int | None = None
    candidate_symbol_open_orders_certified_absent: bool = False
    account_wide_completeness_certified: bool = False
    private_cross_margin_ready: bool = False
    margin_mode_change_required_or_allowed: bool = False
    existing_positions_untouched: bool = True
    native_stop_protection_verified: bool = False
    entry_allowed: bool = False
    live_authorization: bool = False
    mexc_orders_sent: bool = False
    read_only: bool = True


def _fingerprint_positions(rows: Any, symbol: str) -> str:
    if type(rows) is not list or len(rows) > 25:
        raise ValueError("BAD_POSITION_LIST")
    items = []
    identities = set()
    for item in rows:
        if (
            type(item) is not dict or item.get("symbol") != symbol
            or type(item.get("positionType")) is not int
            or item["positionType"] not in (1, 2)
            or type(item.get("openType")) is not int
            or item["openType"] not in (1, 2)
        ):
            raise ValueError("POSITION_SCHEMA_INVALID")
        key = (item.get("positionType"), item.get("positionId", item.get("id")))
        if key in identities:
            raise ValueError("DUPLICATE_POSITION")
        identities.add(key)
        items.append({k: item.get(k) for k in _POSITION_KEYS})
    return json.dumps(
        sorted(items, key=lambda x: json.dumps(x, sort_keys=True, default=str)),
        sort_keys=True, separators=(",", ":"), allow_nan=False,
    )


async def inspect_candidate_position_conflicts(
    private: ReadOnlyMexcProbe, symbol: str,
) -> CandidatePositionConflict:
    def blocked(status: str, **kwargs: Any) -> CandidatePositionConflict:
        return CandidatePositionConflict(status=status, **kwargs)

    if type(symbol) is not str or _SYMBOL.fullmatch(symbol) is None:
        return blocked("CANDIDATE_POSITION_SYMBOL_INVALID")
    if not isinstance(private, ReadOnlyMexcProbe):
        return blocked("CANDIDATE_POSITION_CLIENT_INVALID")

    observations: list[list[dict[str, Any]]] = []
    order_counts: list[dict[str, int]] = []
    for _ in range(2):
        try:
            positions = await private.raw_open_positions(symbol)
            _fingerprint_positions(positions, symbol)
            observations.append(positions)
        except Exception:
            return blocked("CANDIDATE_POSITION_GET_OR_SCHEMA_FAILED")
        try:
            counts = await private.raw_open_order_counts()
            order_counts.append(counts)
        except Exception:
            return blocked("ACCOUNT_ORDER_COUNTS_GET_OR_SCHEMA_FAILED")

    try:
        if _fingerprint_positions(observations[0], symbol) != _fingerprint_positions(
            observations[1], symbol
        ):
            return blocked("CANDIDATE_POSITION_CRITICAL_DRIFT")
        if any(order_counts[0][k] != order_counts[1][k] for k in _COUNT_KEYS):
            return blocked("ACCOUNT_ORDER_COUNT_DRIFT",
                           symbol_scoped_positions_two_get_consistent=True)
    except (ValueError, TypeError, KeyError):
        return blocked("CANDIDATE_POSITION_OR_ORDERS_SCHEMA_UNVERIFIED")

    rows = observations[1]
    shorts = [r for r in rows if r["positionType"] == 2]
    # A position side with >1 row needs ownership/collateral analysis.
    if len(shorts) > 1:
        return blocked("CANDIDATE_MULTIPLE_SHORT_POSITIONS_UNVERIFIED",
                       symbol_scoped_positions_two_get_consistent=True)
    mode = (
        "ISOLATED" if shorts and shorts[0]["openType"] == 1
        else "CROSS" if shorts and shorts[0]["openType"] == 2
        else "NONE"
    )
    common = dict(
        symbol_scoped_positions_two_get_consistent=True,
        candidate_open_positions_observed=len(rows),
        candidate_short_position_observed=bool(shorts),
        candidate_short_mode=mode,
        candidate_no_open_positions_observed=len(rows) == 0,
        account_order_counts_two_get_consistent=True,
        account_open_orders_observed=order_counts[1]["sumCount"],
    )
    if rows:
        return blocked("CANDIDATE_EXISTING_POSITION_PRESENT_NONAUTHORIZING", **common)
    if order_counts[1]["sumCount"] > 0:
        return blocked(
            "CANDIDATE_NO_POSITION_ACCOUNT_ORDERS_PRESENT_UNSCOPED",
            **common,
        )
    return blocked("CANDIDATE_NO_POSITION_ACCOUNT_ORDER_FREE_OBSERVED_NONAUTHORIZING",
                   **common)
