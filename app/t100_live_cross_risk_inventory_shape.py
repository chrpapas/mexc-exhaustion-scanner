"""Independent GET-only account-inventory shape diagnosis for T100/V40.

A healthy FLAT account cannot be passed to the NONFLAT 1–4 position cross
margin witness. Instead of fabricating a portfolio, classify two stable
account-wide position/current TP-SL reads with twice-checked open-order
counts. This is an OBSERVATION only, never position ownership, completeness,
risk adequacy, or trade authorization.

Output is strictly allowlisted counts/booleans; never symbols, IDs,
prices, balances, or raw exchange errors. Private GET only. No POST.
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

from app.t100_live_consistency import CRITICAL_POSITION_FIELDS, CRITICAL_STOP_FIELDS
from app.t100_live_readonly_probe import ReadOnlyMexcProbe


@dataclass(frozen=True)
class InventoryShapeObservation:
    status: str
    observed_positions: int | None = None
    observed_current_stops: int | None = None
    reported_stop_orders: int | None = None
    reported_total_open_orders: int | None = None
    stable_across_reads: bool = False
    order_counts_match: bool = False
    account_wide_completeness_certified: bool = False
    protected_position_ownership_verified: bool = False
    entry_allowed: bool = False
    live_authorization: bool = False


def _canonical(rows: list[dict[str, Any]], keys: tuple[str, ...]) -> tuple[str, ...]:
    return tuple(sorted(json.dumps(
        {k: row[k] for k in keys if k in row},
        sort_keys=True, separators=(",", ":"), allow_nan=False,
    ) for row in rows))


async def observe_inventory_shape(probe: ReadOnlyMexcProbe) -> InventoryShapeObservation:
    def blocked(reason: str) -> InventoryShapeObservation:
        return InventoryShapeObservation(status=reason)

    if not isinstance(probe, ReadOnlyMexcProbe):
        return blocked("INVENTORY_SHAPE_PROBE_INVALID")
    try:
        before_p = await probe.raw_open_positions()
        before_s = await probe.raw_open_stops()
        before_c = await probe.raw_open_order_counts()
        after_p = await probe.raw_open_positions()
        after_s = await probe.raw_open_stops()
        after_c = await probe.raw_open_order_counts()
        same = (
            _canonical(before_p, CRITICAL_POSITION_FIELDS)
            == _canonical(after_p, CRITICAL_POSITION_FIELDS)
            and _canonical(before_s, CRITICAL_STOP_FIELDS)
            == _canonical(after_s, CRITICAL_STOP_FIELDS)
            and before_c == after_c
        )
        if not same:
            return blocked("INVENTORY_SHAPE_ACCOUNT_STATE_DRIFT")
        n_positions = len(after_p)
        n_stops = len(after_s)
        total = after_c["sumCount"]
        reported_stops = after_c["stopOrderCount"]
        # The total count sums across several order types. Do not infer a
        # flat, order-free account from zero positions and TP/SL alone.
        if reported_stops != n_stops:
            reason = "INVENTORY_SHAPE_STOP_COUNT_DISAGREES_WITH_EXCHANGE"
        elif n_positions == 0 and n_stops == 0 and total == 0:
            reason = "INVENTORY_SHAPE_FLAT_AND_ORDER_FREE_OBSERVED_NONATOMIC"
        elif n_positions == 0 and n_stops == 0:
            reason = "INVENTORY_SHAPE_FLAT_WITH_OPEN_ORDERS"
        elif n_positions == 0:
            reason = "INVENTORY_SHAPE_STOPS_WITHOUT_POSITION"
        elif n_positions > 4:
            reason = "INVENTORY_SHAPE_MORE_THAN_FOUR_POSITIONS"
        elif n_stops != n_positions:
            # Multiple native TP/SL rows may legitimately protect one
            # position, but the existing V40 nonflat witness cannot
            # certify them until an independent coverage audit is added.
            reason = "INVENTORY_SHAPE_NONFLAT_STOP_ROW_COUNT_DIFFERS"
        else:
            reason = "INVENTORY_SHAPE_NONFLAT_COUNTS_MATCH_NONAUTHORIZING"
        return InventoryShapeObservation(
            status=reason, observed_positions=n_positions,
            observed_current_stops=n_stops,
            reported_stop_orders=reported_stops,
            reported_total_open_orders=total,
            stable_across_reads=True,
            order_counts_match=(reported_stops == n_stops),
        )
    except Exception:
        # Never expose exception messages (including authenticated request
        # URL, exchange body or possibly sensitive account IDs).
        return blocked("INVENTORY_SHAPE_READ_UNVERIFIED")
