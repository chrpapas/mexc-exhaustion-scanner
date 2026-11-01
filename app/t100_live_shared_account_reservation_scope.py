"""Pure account-wide pre-reservation scope for shared manual/T100 V40.

No order authorization, venue GET, DB write, or ownership adoption. A
bookended hash from the GET/SELECT coordinator must cover all positions
and TP/SL orders, including stopless operator positions. T100 ownership
comes ONLY from complete durable recovery. This scope cannot detect any
exchange change after the supplied snapshot or attest wallet collateral.
"""
from __future__ import annotations

from dataclasses import dataclass
import re
from typing import Any

from app.t100_live_four_slot_snapshot_coordinator import (
    FourSlotSnapshotResult, _valid_counts, _venue_digest,
)
# Independent of the post-entry observer: importing it would create a
# reservation -> attempt journal -> reservation circular import.


def _id(value: Any) -> str:
    if value is None or type(value) is bool:
        raise ValueError("SHARED_INVALID_ID")
    result = str(value)
    if not result or result in {"0", "None", "null"}:
        raise ValueError("SHARED_INVALID_ID")
    return result


def _map_positions(rows: Any) -> dict[str, dict[str, Any]]:
    if type(rows) is not list or any(type(row) is not dict for row in rows):
        raise ValueError("SHARED_POSITIONS_INVALID")
    positions: dict[str, dict[str, Any]] = {}
    symbols: set[str] = set()
    for row in rows:
        pid = _id(row.get("positionId") or row.get("id"))
        symbol = row.get("symbol")
        if (
            pid in positions or type(symbol) is not str
            or re.fullmatch("[A-Z0-9]+_USDT", symbol) is None
            or symbol in symbols
            or type(row.get("positionType")) is not int
            or row["positionType"] not in {1, 2}
            or type(row.get("openType")) is not int
            or row["openType"] not in {1, 2}
        ):
            raise ValueError("SHARED_POSITION_IDENTITY_INVALID")
        positions[pid] = row
        symbols.add(symbol)
    return positions


def _map_stops(
    rows: Any, positions: dict[str, dict[str, Any]],
) -> dict[str, list[dict[str, Any]]]:
    if type(rows) is not list or any(type(row) is not dict for row in rows):
        raise ValueError("SHARED_STOPS_INVALID")
    observed: dict[str, list[dict[str, Any]]] = {}
    stop_ids: set[str] = set()
    for row in rows:
        sid = _id(row.get("id") or row.get("stopPlanOrderId"))
        pid = _id(row.get("positionId"))
        if (
            sid in stop_ids or pid not in positions
            or row.get("symbol") not in (None, positions[pid]["symbol"])
        ):
            raise ValueError("SHARED_STOP_IDENTITY_INVALID")
        stop_ids.add(sid)
        observed.setdefault(pid, []).append(row)
    return observed



class SharedAccountScopeHalted(RuntimeError):
    pass


@dataclass(frozen=True)
class SharedAccountScope:
    manual_symbols: frozenset[str]
    owned_symbols: frozenset[str]
    all_symbols: frozenset[str]
    position_count: int
    manual_count: int
    owned_count: int
    nonauthorizing: bool = True


def inspect_shared_account_scope(
    witness: FourSlotSnapshotResult,
    pre_positions: Any,
    pre_stops: Any,
) -> SharedAccountScope:
    def fail(code: str) -> None:
        raise SharedAccountScopeHalted(code)

    if (
        type(witness) is not FourSlotSnapshotResult
        or witness.status not in {
            "SHARED_ACCOUNT_MANUAL_ONLY_NONAUTHORIZING",
            "SHARED_ACCOUNT_MANAGED_PROVENANCE_MATCHED_NONAUTHORIZING",
        }
        or witness.entry_allowed is not False
        or witness.live_authorization is not False
        or type(witness.occupied_slots) is not int
        or type(witness.account_positions_observed) is not int
        or type(witness.manual_positions_observed) is not int
        or not 0 <= witness.occupied_slots <= 4
        or not 0 <= witness.manual_positions_observed <= 4
        or not 0 <= witness.account_positions_observed <= 4
        or witness.account_positions_observed != (
            witness.occupied_slots + witness.manual_positions_observed
        )
        or not isinstance(witness.ledger_hash, str)
        or re.fullmatch("[0-9a-f]{64}", witness.ledger_hash) is None
        or not isinstance(witness.critical_inventory_hash, str)
        or re.fullmatch("[0-9a-f]{64}", witness.critical_inventory_hash) is None
        or type(pre_positions) is not list
        or type(pre_stops) is not list
        or len(pre_positions) != witness.account_positions_observed
        or not _valid_counts(witness.inventory, len(pre_positions), len(pre_stops))
    ):
        fail("SHARED_SCOPE_WITNESS_OR_COUNTS_INVALID")
    try:
        positions = _map_positions(pre_positions)
        stops = _map_stops(pre_stops, positions)
        if _venue_digest(pre_positions, pre_stops) != witness.critical_inventory_hash:
            fail("SHARED_SCOPE_INVENTORY_HASH_DRIFT")
    except (TypeError, ValueError, KeyError):
        fail("SHARED_SCOPE_EXCHANGE_ROWS_UNVERIFIED")

    owned: set[str] = set()
    if witness.occupied_slots == 0:
        if (
            witness.recovery is not None
            or witness.status != "SHARED_ACCOUNT_MANUAL_ONLY_NONAUTHORIZING"
        ):
            fail("SHARED_SCOPE_MANAGED_LEDGER_UNEXPECTED")
    else:
        recovery = witness.recovery
        if (
            recovery is None
            or witness.status != "SHARED_ACCOUNT_MANAGED_PROVENANCE_MATCHED_NONAUTHORIZING"
            or recovery.mode != "SHADOW_ONLY_RECOVERED_NONFLAT"
            or recovery.live_authorization is not False
            or type(recovery.recovered) is not tuple
            or len(recovery.recovered) != witness.occupied_slots
            or recovery.checkpoint_count != witness.occupied_slots
            or recovery.exchange_position_count != witness.occupied_slots
            or recovery.exchange_stop_count != witness.occupied_slots
        ):
            fail("SHARED_SCOPE_MANAGED_RECOVERY_INVALID")
        ids: set[str] = set()
        stop_ids: set[str] = set()
        for cp in recovery.recovered:
            pid = cp.position_id
            position = positions.get(pid)
            matches = stops.get(pid, [])
            if (
                pid in ids
                or cp.stop_order_id in stop_ids
                or position is None
                or position.get("symbol") != cp.symbol
                or position.get("positionType") != 2
                or position.get("openType") != 2
                or len(matches) != 1
                or str(matches[0].get("id") or matches[0].get("stopPlanOrderId"))
                   != cp.stop_order_id
            ):
                fail("SHARED_SCOPE_MANAGED_POSITION_OR_STOP_DRIFT")
            ids.add(pid)
            stop_ids.add(cp.stop_order_id)
            owned.add(cp.symbol)
    manual = {
        row["symbol"] for pid, row in positions.items()
        if not witness.recovery or pid not in {
            entry.position_id for entry in witness.recovery.recovered
        }
    }
    if len(manual) != witness.manual_positions_observed or len(owned | manual) != len(positions):
        fail("SHARED_SCOPE_MANUAL_PARTITION_UNVERIFIED")
    return SharedAccountScope(
        manual_symbols=frozenset(manual),
        owned_symbols=frozenset(owned),
        all_symbols=frozenset(manual | owned),
        position_count=len(positions),
        manual_count=len(manual),
        owned_count=len(owned),
    )
