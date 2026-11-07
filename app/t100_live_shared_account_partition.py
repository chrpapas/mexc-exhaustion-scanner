"""Pure, nonauthorizing partition of one stable MEXC account inventory.

Partitions current position and TP/SL rows by *durably recorded position ID*.
External/manual rows remain visible for shared CROSS collateral/risk accounting
but are NEVER turned into T100 checkpoint rows, protected stops, or orders.

Only the downstream full durable recovery function may substantiate owned T100
positions; this partition does not authorize trading or prove cross liquidation.
"""
from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
import re
from typing import Any, Sequence

from app.t100_live_durable_recovery import PositionCheckpoint


class SharedPartitionHalted(RuntimeError):
    pass


@dataclass(frozen=True)
class SharedAccountPartition:
    managed_positions: list[dict[str, Any]]
    managed_stops: list[dict[str, Any]]
    manual_positions: int
    manual_current_tpsl_rows: int
    manual_positions_without_positive_native_sl: int
    account_positions: int
    account_current_tpsl_rows: int
    entry_allowed: bool = False
    live_authorization: bool = False


def _id(raw: Any) -> str:
    """Require the same positive ASCII venue identity as the durable journal.

    Do not silently normalize whitespace, signs, leading zeroes, Unicode
    digits, floats or booleans into apparent ownership. Missing/conflicting
    external identities are a failed observation, never "manual" proof.
    """
    if type(raw) not in (str, int):
        raise SharedPartitionHalted("SHARED_ACCOUNT_IDENTITY_AMBIGUOUS")
    value = str(raw)
    if re.fullmatch(r"[1-9][0-9]*", value) is None:
        raise SharedPartitionHalted("SHARED_ACCOUNT_IDENTITY_AMBIGUOUS")
    return value


def _venue_id(row: dict[str, Any], primary: str, alternate: str | None = None) -> str:
    """Reject a malformed primary instead of falling back to a valid alias."""
    if primary in row:
        value = _id(row[primary])
        if alternate is not None and alternate in row:
            if _id(row[alternate]) != value:
                raise SharedPartitionHalted("SHARED_ACCOUNT_IDENTITY_CONFLICT")
        return value
    if alternate is not None and alternate in row:
        return _id(row[alternate])
    raise SharedPartitionHalted("SHARED_ACCOUNT_IDENTITY_AMBIGUOUS")


def _positive(raw: Any) -> bool:
    if raw is None or isinstance(raw, bool):
        return False
    try:
        n = Decimal(str(raw))
    except (InvalidOperation, ValueError, TypeError):
        return False
    return n.is_finite() and n > 0


def partition_shared_account(
    *, checkpoints: Sequence[PositionCheckpoint],
    positions: Any, stops: Any,
) -> SharedAccountPartition:
    """Partition only; requires independently bookended account GET evidence."""
    if (
        type(checkpoints) not in (list, tuple)
        or any(type(cp) is not PositionCheckpoint for cp in checkpoints)
        or type(positions) is not list or type(stops) is not list
        or any(type(row) is not dict for row in (*positions, *stops))
        or len(checkpoints) > 4 or len(positions) > 4
        or len(stops) > 2000
    ):
        raise SharedPartitionHalted("SHARED_ACCOUNT_ROWS_OR_SLOTS_INVALID")

    cp_by_id: dict[str, PositionCheckpoint] = {}
    cp_by_symbol: dict[str, PositionCheckpoint] = {}
    for cp in checkpoints:
        pid = _id(cp.position_id)
        if pid in cp_by_id or cp.symbol in cp_by_symbol:
            raise SharedPartitionHalted("SHARED_CHECKPOINT_DUPLICATE_IDENTITY")
        cp_by_id[pid], cp_by_symbol[cp.symbol] = cp, cp

    by_position: dict[str, dict[str, Any]] = {}
    managed_positions: list[dict[str, Any]] = []
    manual_position_ids: set[str] = set()
    for row in positions:
        pid = _venue_id(row, "positionId")
        symbol = row.get("symbol")
        if pid in by_position or not isinstance(symbol, str) or not symbol:
            raise SharedPartitionHalted("SHARED_POSITION_IDENTITY_AMBIGUOUS")
        by_position[pid] = row
        cp = cp_by_id.get(pid)
        if cp is not None:
            if cp.symbol != symbol:
                raise SharedPartitionHalted("SHARED_CHECKPOINT_POSITION_SYMBOL_DRIFT")
            managed_positions.append(row)
        else:
            # Never shadow T100 ownership with an extra manual position in
            # the SAME symbol (e.g., opposite hedge side): future stop/close
            # actions would risk affecting a manual position.
            if symbol in cp_by_symbol:
                raise SharedPartitionHalted("SHARED_MANUAL_SYMBOL_CONFLICT")
            manual_position_ids.add(pid)

    if len(managed_positions) != len(checkpoints):
        raise SharedPartitionHalted("SHARED_MANAGED_POSITION_MISSING")

    managed_stops: list[dict[str, Any]] = []
    manual_stops = 0
    manual_with_positive_sl: set[str] = set()
    seen_stop_ids: set[str] = set()
    for row in stops:
        sid = _venue_id(row, "id", "stopPlanOrderId")
        pid = _venue_id(row, "positionId")
        if sid in seen_stop_ids or pid not in by_position:
            raise SharedPartitionHalted("SHARED_TPSL_DUPLICATE_OR_ORPHAN")
        seen_stop_ids.add(sid)
        symbol = row.get("symbol")
        if symbol is not None and symbol != by_position[pid].get("symbol"):
            raise SharedPartitionHalted("SHARED_TPSL_SYMBOL_DRIFT")
        if pid in cp_by_id:
            managed_stops.append(row)
        else:
            manual_stops += 1
            if _positive(row.get("stopLossPrice")):
                manual_with_positive_sl.add(pid)

    if len(managed_stops) != len(checkpoints):
        raise SharedPartitionHalted("SHARED_MANAGED_STOP_COUNT_MISMATCH")

    # The downstream durable recovery checks exact T100 stop IDs, prices,
    # protected quantity, states, original fills and revision history.
    return SharedAccountPartition(
        managed_positions=managed_positions,
        managed_stops=managed_stops,
        manual_positions=len(manual_position_ids),
        manual_current_tpsl_rows=manual_stops,
        manual_positions_without_positive_native_sl=(
            len(manual_position_ids - manual_with_positive_sl)
        ),
        account_positions=len(positions),
        account_current_tpsl_rows=len(stops),
    )
