"""Read-only scoped candidate regular-limit-order witness, current snapshot only.

MEXC: GET /api/v1/private/order/list/open_orders page_num=1,page_size=100.
No documented symbol filter on this current endpoint, so the result is
account-wide and filtered IN MEMORY after checking count parity. Never
assume one page is complete unless the bounded account limit-order count
matches the fetched unique rows. Two position/COUNT/order/COUNT windows.
No credentials, raw orders, symbol names, IDs, amounts or prices are returned.
This is not account-wide atomicity, exchange risk certification or permission
for any order or other mutation.
"""
from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from typing import Any

from app.t100_live_readonly_probe import ReadOnlyMexcProbe

_SYMBOL = re.compile(r"[A-Z0-9]+_USDT")
_PAGE = "/api/v1/private/order/list/open_orders"
_ORDER_FIELDS = (
    "orderId", "symbol", "positionId", "price", "priceStr",
    "vol", "dealVol", "orderMargin", "side", "category",
    "orderType", "openType", "state", "externalOid",
    "version", "createTime", "updateTime", "reduceOnly",
)
_POSITION_FIELDS = (
    "positionId", "id", "symbol", "positionType", "openType",
    "state", "holdVol", "frozenVol", "version", "updateTime",
)
_COUNT_FIELDS = (
    "sumCount", "limitOrderCount", "stopOrderCount",
    "planOrderCount", "trackOrderCount",
)


@dataclass(frozen=True)
class CandidateLimitOrderWitness:
    status: str
    position_two_reads_consistent: bool = False
    candidate_positions_observed: int | None = None
    counts_before_after_consistent: bool = False
    limit_order_list_two_reads_consistent: bool = False
    account_limit_orders_reported: int | None = None
    account_limit_orders_listed: int | None = None
    candidate_limit_orders_observed: int | None = None
    other_contract_limit_orders_observed: int | None = None
    account_limit_order_count_matches_list: bool = False
    candidate_regular_limit_absent_observed: bool = False
    candidate_all_order_types_absent_certified: bool = False
    account_wide_completeness_certified: bool = False
    private_cross_margin_ready: bool = False
    collateral_certified: bool = False
    native_stop_protection_verified: bool = False
    entry_allowed: bool = False
    live_authorization: bool = False
    mexc_orders_sent: bool = False
    read_only: bool = True


def _canonical_orders(data: Any) -> tuple[str, tuple[dict[str, Any], ...]]:
    if type(data) is not list or len(data) > 100:
        raise ValueError("ORDER_PAGE_SHAPE")
    rows: list[dict[str, Any]] = []
    seen: set[str] = set()
    for order in data:
        if type(order) is not dict:
            raise ValueError("ORDER_ROW_SHAPE")
        oid = order.get("orderId")
        sym = order.get("symbol")
        if (
            type(oid) not in (int, str)
            or not str(oid).isdigit()
            or int(oid) <= 0
            or str(oid) in seen
            or type(sym) is not str
            or _SYMBOL.fullmatch(sym) is None
            or type(order.get("category")) is not int
            or order["category"] != 1
            or type(order.get("orderType")) is not int
            or order["orderType"] not in (1, 2, 3, 4)
            or type(order.get("side")) is not int
            or order["side"] not in (1, 2, 3, 4)
            or type(order.get("state")) is not int
            or order["state"] not in (1, 2)
            or type(order.get("openType")) is not int
            or order["openType"] not in (1, 2)
        ):
            raise ValueError("ORDER_ROW_UNVERIFIED")
        seen.add(str(oid))
        rows.append(order)
    content = [
        {key: row.get(key) for key in _ORDER_FIELDS}
        for row in rows
    ]
    content.sort(key=lambda x: str(x["orderId"]))
    blob = json.dumps(content, separators=(",", ":"), sort_keys=True, allow_nan=False)
    return hashlib.sha256(blob.encode()).hexdigest(), tuple(rows)


def _canonical_positions(data: Any, symbol: str) -> str:
    if type(data) is not list or len(data) > 20:
        raise ValueError("POSITION_COLLECTION_INVALID")
    content = []
    for row in data:
        if type(row) is not dict or row.get("symbol") != symbol:
            raise ValueError("POSITION_SCOPE_UNVERIFIED")
        content.append({key: row.get(key) for key in _POSITION_FIELDS})
    content.sort(key=lambda r: json.dumps(r, sort_keys=True, default=str))
    return hashlib.sha256(
        json.dumps(content, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()
    ).hexdigest()


async def inspect_candidate_limit_orders(
    private: ReadOnlyMexcProbe, symbol: str,
) -> CandidateLimitOrderWitness:
    def blocked(status: str, **kwargs: Any) -> CandidateLimitOrderWitness:
        return CandidateLimitOrderWitness(status=status, **kwargs)

    if type(symbol) is not str or _SYMBOL.fullmatch(symbol) is None:
        return blocked("CANDIDATE_LIMIT_SYMBOL_INVALID")
    if not isinstance(private, ReadOnlyMexcProbe):
        return blocked("CANDIDATE_LIMIT_CLIENT_INVALID")
    client = private._client
    if not callable(getattr(client, "_private_get", None)):
        return blocked("CANDIDATE_LIMIT_CLIENT_INVALID")

    snapshots = []
    for _ in range(2):
        try:
            positions = await private.raw_open_positions(symbol)
            before = await private.raw_open_order_counts()
            if before["limitOrderCount"] > 100:
                return blocked("CANDIDATE_LIMIT_PAGINATION_NOT_BOUNDED")
            rows = await client._private_get(
                _PAGE, {"page_num": 1, "page_size": 100},
            )
            after = await private.raw_open_order_counts()
        except Exception:
            return blocked("CANDIDATE_LIMIT_GET_FAILED")
        try:
            pos_hash = _canonical_positions(positions, symbol)
            order_hash, validated = _canonical_orders(rows)
            if any(before[k] != after[k] for k in _COUNT_FIELDS):
                return blocked("CANDIDATE_LIMIT_COUNT_DRIFT")
            if len(validated) != before["limitOrderCount"]:
                return blocked("CANDIDATE_LIMIT_PAGE_COUNT_MISMATCH")
        except (ValueError, TypeError, KeyError):
            return blocked("CANDIDATE_LIMIT_SCHEMA_UNVERIFIED")
        snapshots.append((positions, before, validated, pos_hash, order_hash))

    first, last = snapshots
    if first[3] != last[3]:
        return blocked("CANDIDATE_LIMIT_POSITION_DRIFT")
    if any(first[1][k] != last[1][k] for k in _COUNT_FIELDS):
        return blocked("CANDIDATE_LIMIT_ACCOUNT_ORDER_COUNT_DRIFT")
    if first[4] != last[4]:
        return blocked("CANDIDATE_LIMIT_ORDER_LIST_DRIFT")
    positions, counts, orders, _, _ = last
    own = sum(1 for item in orders if item["symbol"] == symbol)
    others = len(orders) - own
    common = dict(
        position_two_reads_consistent=True,
        candidate_positions_observed=len(positions),
        counts_before_after_consistent=True,
        limit_order_list_two_reads_consistent=True,
        account_limit_orders_reported=counts["limitOrderCount"],
        account_limit_orders_listed=len(orders),
        candidate_limit_orders_observed=own,
        other_contract_limit_orders_observed=others,
        account_limit_order_count_matches_list=True,
        candidate_regular_limit_absent_observed=own == 0,
    )
    if positions:
        return blocked("CANDIDATE_LIMIT_POSITION_PRESENT_NONAUTHORIZING", **common)
    if own:
        return blocked("CANDIDATE_LIMIT_CANDIDATE_ORDER_PRESENT_NONAUTHORIZING", **common)
    if counts["sumCount"] != counts["limitOrderCount"]:
        return blocked("CANDIDATE_LIMIT_OTHER_ORDER_TYPES_UNSCOPED", **common)
    return blocked("CANDIDATE_LIMIT_ONLY_OTHER_CONTRACT_OBSERVED_NONAUTHORIZING",
                   **common)
