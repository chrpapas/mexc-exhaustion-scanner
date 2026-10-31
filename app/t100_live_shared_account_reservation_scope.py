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
from app.t100_live_shared_protected_entry_observer import (
    _map_positions, _map_stops,
)


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
