"""Pure read-only MEXC inventory snapshot validator.

Never infers 'flat' from absent, partial or malformed data. A caller must
supply complete paginated exchange reads and their observation timestamps.
This is not authorization to send orders.
"""
from __future__ import annotations
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from typing import Any


class SnapshotHalted(RuntimeError):
    pass


@dataclass(frozen=True)
class InventorySnapshot:
    observed_at_ms: int
    position_count: int
    stop_count: int
    total_short_contracts: Decimal
    flat: bool


def _positive(raw: object, field: str) -> Decimal:
    try:
        value = Decimal(str(raw))
    except (InvalidOperation, TypeError, ValueError) as exc:
        raise SnapshotHalted(f"INVALID_{field}") from exc
    if not value.is_finite() or value <= 0:
        raise SnapshotHalted(f"INVALID_{field}")
    return value


def _integer(raw: object, field: str) -> int:
    if isinstance(raw, bool):
        raise SnapshotHalted(f"INVALID_{field}")
    try:
        value = int(str(raw))
    except (ValueError, TypeError) as exc:
        raise SnapshotHalted(f"INVALID_{field}") from exc
    if str(value) != str(raw):
        raise SnapshotHalted(f"INVALID_{field}")
    return value


def validate_inventory_snapshot(
    *, positions: Any, stops: Any, observed_at_ms: int,
    now_ms: int, positions_complete: bool, stops_complete: bool,
    max_age_ms: int = 1500,
) -> InventorySnapshot:
    if positions_complete is not True or stops_complete is not True:
        raise SnapshotHalted("INVENTORY_PAGINATION_INCOMPLETE")
    if not isinstance(positions, list) or not isinstance(stops, list):
        raise SnapshotHalted("INVENTORY_RESPONSE_MALFORMED")
    if not isinstance(observed_at_ms, int) or isinstance(observed_at_ms, bool):
        raise SnapshotHalted("INVALID_SNAPSHOT_TIMESTAMP")
    if not isinstance(now_ms, int) or isinstance(now_ms, bool):
        raise SnapshotHalted("INVALID_SNAPSHOT_TIMESTAMP")
    if not 0 <= now_ms - observed_at_ms <= max_age_ms <= 2000:
        raise SnapshotHalted("INVENTORY_SNAPSHOT_STALE_OR_FUTURE")
    position_ids: set[tuple[str, str]] = set()
    stop_ids: set[str] = set()
    total_short = Decimal(0)
    for position in positions:
        if not isinstance(position, dict):
            raise SnapshotHalted("MALFORMED_POSITION")
        symbol = position.get("symbol")
        pid = str(position.get("positionId") or position.get("id") or "")
        if not isinstance(symbol, str) or not symbol or not pid:
            raise SnapshotHalted("MISSING_POSITION_IDENTITY")
        key = (symbol, pid)
        if key in position_ids:
            raise SnapshotHalted("DUPLICATE_POSITION")
        position_ids.add(key)
        side = _integer(position.get("positionType"), "POSITION_SIDE")
        if side not in {1, 2}:
            raise SnapshotHalted("UNKNOWN_POSITION_SIDE")
        state = _integer(position.get("state"), "POSITION_STATE")
        if state != 1:
            # MEXC documents: 1 holding, 2 system-held, 3 closed.
            # Only an ordinary holding state is acceptable to the shadow
            # lifecycle parser; all other states require operator review.
            raise SnapshotHalted("POSITION_NOT_ORDINARY_HOLDING")
        qty = _positive(position.get("holdVol"), "POSITION_VOLUME")
        if side == 2:
            total_short += qty
    for stop in stops:
        if not isinstance(stop, dict):
            raise SnapshotHalted("MALFORMED_STOP")
        sid = str(stop.get("id") or stop.get("stopPlanOrderId") or "")
        if not sid or not stop.get("positionId"):
            raise SnapshotHalted("MISSING_STOP_IDENTITY")
        if sid in stop_ids:
            raise SnapshotHalted("DUPLICATE_STOP")
        stop_ids.add(sid)
        _positive(stop.get("stopLossPrice"), "STOP_PRICE")
        _positive(stop.get("vol"), "STOP_VOLUME")
        state = _integer(stop.get("state"), "STOP_STATE")
        if state != 1:
            raise SnapshotHalted("INACTIVE_STOP_IN_OPEN_INVENTORY")
    return InventorySnapshot(
        observed_at_ms, len(positions), len(stops), total_short,
        not positions and not stops,
    )
