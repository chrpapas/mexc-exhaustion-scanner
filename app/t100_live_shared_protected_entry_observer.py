"""Read-only, fail-closed post-entry witness for a mixed manual/T100 account.

Purpose: validate an ALREADY STAGED (not submitted here) V40 entry using
bookended FULL-ACCOUNT GETs. Operator/manual positions and any manual TP/SL
must be unchanged in safety-critical fields. Existing owned protected
positions are also preserved. The ONE new entry must have final/complete
fills and an independently observed full-volume exchange-native SL.

This cannot place or retry orders, repair stops, approve collateral, prove
exchange atomicity, write a checkpoint, or authorize production execution.
Do not wire it to LiveOrderGateway or Render. A missing externalOid result
after an ambiguous POST NEVER gives permission to retry the POST.
"""
from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from typing import Any, Protocol

from app.t100_live_active_tpsl import effective_protective_quantity
from app.t100_live_execution import EntryIntent
from app.t100_live_four_slot_attempt_journal import DurableAttemptStage
from app.t100_live_four_slot_snapshot_coordinator import (
    FourSlotSnapshotResult, _hash_rows, _valid_counts, _venue_digest,
)

D = Decimal


@dataclass(frozen=True)
class SharedProtectedEntryObservation:
    status: str
    preexisting_manual_positions: int | None = None
    preexisting_owned_positions: int | None = None
    total_positions_after: int | None = None
    new_t100_full_fill_and_native_stop_observed: bool = False
    legacy_manual_inventory_preserved: bool = False
    journal_ack_persisted: bool = False
    durable_checkpoint_persisted: bool = False
    exchange_atomicity_verified: bool = False
    shared_cross_collateral_certified: bool = False
    native_stop_execution_certified: bool = False
    entry_allowed: bool = False
    live_authorization: bool = False
    mexc_orders_sent: bool = False
    read_only: bool = True


def _id(x: Any) -> str:
    if x is None or type(x) is bool:
        raise ValueError("INVALID_ID")
    z = str(x)
    if not z or z in {"0", "None", "null"}:
        raise ValueError("INVALID_ID")
    return z


def _decimal(x: Any) -> D:
    if x is None or type(x) is bool:
        raise ValueError("INVALID_AMOUNT")
    try:
        z = D(str(x))
    except (InvalidOperation, ValueError, TypeError):
        raise ValueError("INVALID_AMOUNT") from None
    if not z.is_finite() or z <= 0:
        raise ValueError("INVALID_AMOUNT")
    return z


def _map_positions(rows: Any) -> dict[str, dict[str, Any]]:
    if type(rows) is not list or any(type(p) is not dict for p in rows):
        raise ValueError("INVALID_POSITIONS")
    d: dict[str, dict[str, Any]] = {}
    seen_symbols: set[str] = set()
    for p in rows:
        pid = _id(p.get("positionId") or p.get("id"))
        sym = p.get("symbol")
        if (
            pid in d or not isinstance(sym, str) or not sym
            or sym in seen_symbols
            or type(p.get("positionType")) is not int
            or p["positionType"] not in {1, 2}
            or type(p.get("openType")) is not int
            or p["openType"] not in {1, 2}
        ):
            raise ValueError("POSITION_IDENTITY_DUPLICATE_OR_UNVERIFIED")
        d[pid] = p
        seen_symbols.add(sym)
    return d


def _map_stops(rows: Any, known: dict[str, dict[str, Any]]) -> dict[str, list[dict[str, Any]]]:
    if type(rows) is not list or any(type(s) is not dict for s in rows):
        raise ValueError("INVALID_STOPS")
    by_pid: dict[str, list[dict[str, Any]]] = {}
    stop_ids: set[str] = set()
    for s in rows:
        sid = _id(s.get("id") or s.get("stopPlanOrderId"))
        pid = _id(s.get("positionId"))
        if (
            sid in stop_ids or pid not in known
            or (s.get("symbol") is not None
                and s["symbol"] != known[pid].get("symbol"))
        ):
            raise ValueError("DUPLICATE_OR_ORPHAN_STOP")
        stop_ids.add(sid)
        by_pid.setdefault(pid, []).append(s)
    return by_pid


def assess_shared_protected_entry(
    *,
    pre_witness: FourSlotSnapshotResult,
    pre_positions: Any,
    pre_stops: Any,
    stage: DurableAttemptStage,
    intent: EntryIntent,
    post_positions: Any,
    post_stops: Any,
    post_order: Any,
    post_inventory: Any,
) -> SharedProtectedEntryObservation:
    """Pure assessment of a complete, consistently read account outcome."""
    def block(code: str, **counts: Any) -> SharedProtectedEntryObservation:
        return SharedProtectedEntryObservation(status=code, **counts)

    if (
        type(pre_witness) is not FourSlotSnapshotResult
        or pre_witness.status not in {
            "SHARED_ACCOUNT_MANUAL_ONLY_NONAUTHORIZING",
            "SHARED_ACCOUNT_MANAGED_PROVENANCE_MATCHED_NONAUTHORIZING",
        }
        or pre_witness.entry_allowed is not False
        or pre_witness.live_authorization is not False
        or type(pre_witness.account_positions_observed) is not int
        or not 0 <= pre_witness.account_positions_observed < 4
        or type(pre_witness.manual_positions_observed) is not int
        or not 0 <= pre_witness.manual_positions_observed < 4
        or type(pre_witness.occupied_slots) is not int
        or pre_witness.account_positions_observed
            != pre_witness.occupied_slots + pre_witness.manual_positions_observed
        or not isinstance(pre_witness.ledger_hash, str)
        or len(pre_witness.ledger_hash) != 64
        or not isinstance(pre_witness.critical_inventory_hash, str)
        or len(pre_witness.critical_inventory_hash) != 64
        or type(stage) is not DurableAttemptStage
        or type(intent) is not EntryIntent
        or stage.state != "SUBMITTING"
        or stage.post_attempted is not False
        or stage.reconciliation_required is not True
        or stage.entry_allowed is not False
        or stage.live_authorization is not False
        or stage.signal_id != intent.signal_id
        or stage.external_oid != intent.external_oid
        or not intent.external_oid
    ):
        return block("SHARED_PROTECTED_PRE_OR_STAGE_UNVERIFIED")

    if (
        not _valid_counts(pre_witness.inventory,
                          pre_witness.account_positions_observed,
                          len(pre_stops) if type(pre_stops) is list else -1)
    ):
        return block("SHARED_PROTECTED_PRE_COMPLETENESS_INVALID")
    try:
        before = _map_positions(pre_positions)
        before_stops = _map_stops(pre_stops, before)
        if (
            len(before) != pre_witness.account_positions_observed
            or _venue_digest(pre_positions, pre_stops)
                != pre_witness.critical_inventory_hash
        ):
            return block("SHARED_PROTECTED_PRE_CRITICAL_HASH_CHANGED")
    except (ValueError, TypeError, KeyError):
        return block("SHARED_PROTECTED_PRE_ROWS_INVALID")

    owned = () if pre_witness.recovery is None else pre_witness.recovery.recovered
    owned_ids = {row.position_id for row in owned}
    if (
        len(owned) != pre_witness.occupied_slots
        or len(owned_ids) != len(owned)
        or (pre_witness.occupied_slots == 0) != (pre_witness.recovery is None)
        or any(row.position_id not in before for row in owned)
        or any(before[row.position_id].get("symbol") != row.symbol for row in owned)
        or any(
            before[row.position_id].get("positionType") != 2
            or before[row.position_id].get("openType") != 2
            for row in owned
        )
    ):
        return block("SHARED_PROTECTED_PRE_OWNED_PROVENANCE_UNVERIFIED")

    if (
        len({row["symbol"] for row in pre_positions}) != len(pre_positions)
        or intent.symbol in {row["symbol"] for row in pre_positions}
    ):
        return block("SHARED_PROTECTED_CANDIDATE_SYMBOL_CONFLICT")

    try:
        after = _map_positions(post_positions)
        after_stops = _map_stops(post_stops, after)
        if (
            len(after) != len(before) + 1
            or len(after) > 4
            or not set(before).issubset(after)
        ):
            return block("SHARED_PROTECTED_POST_POSITION_SET_MISMATCH")
        new_pids = set(after) - set(before)
        if len(new_pids) != 1:
            return block("SHARED_PROTECTED_EXTRA_OR_MISSING_ENTRY")
        new_id = next(iter(new_pids))
        new_position = after[new_id]
        # Any preexisting position and stop, including operator/isolated
        # rows, must remain safety-critically unchanged. MTM is excluded
        # by _venue_digest, not silently treated as a stable fill price.
        previous_positions = [after[pid] for pid in before]
        previous_stops = [
            row for pid in before for row in after_stops.get(pid, [])
        ]
        if _venue_digest(previous_positions, previous_stops) != _venue_digest(
            pre_positions, pre_stops
        ):
            return block("SHARED_PROTECTED_PREEXISTING_ACCOUNT_DRIFT")
        if (
            type(post_order) is not dict
            or post_order.get("externalOid") != intent.external_oid
            or post_order.get("symbol") != intent.symbol
            or post_order.get("side") != 3
            or post_order.get("state") != 3
            or _id(post_order.get("orderId")) == "0"
            or _id(post_order.get("positionId")) != new_id
            or _decimal(post_order.get("dealVol")) != intent.volume_contracts
            or _decimal(post_order.get("dealAvgPrice"))
                != _decimal(new_position.get("holdAvgPrice"))
            or new_position.get("symbol") != intent.symbol
            or new_position.get("positionType") != 2
            or new_position.get("openType") != 2
            or new_position.get("state") != 1
            or _decimal(new_position.get("holdVol")) != intent.volume_contracts
        ):
            return block("SHARED_PROTECTED_FILL_OR_POSITION_NOT_FINAL")
        new_native = after_stops.get(new_id, [])
        if len(new_native) != 1:
            return block("SHARED_PROTECTED_NATIVE_STOP_MISSING_OR_DUPLICATE")
        stop = new_native[0]
        if (
            stop.get("symbol") != intent.symbol
            or stop.get("positionType") != 2
            or stop.get("state") != 1
            or stop.get("isFinished") != 0
            or _decimal(stop.get("stopLossPrice")) > _decimal(intent.stop_price)
            or effective_protective_quantity(
                stop, position_quantity=intent.volume_contracts,
            ) != intent.volume_contracts
        ):
            return block("SHARED_PROTECTED_NATIVE_STOP_INVALID_OR_UNDERCOVERED")
    except (TypeError, ValueError, InvalidOperation, KeyError):
        return block("SHARED_PROTECTED_EXCHANGE_DATA_UNVERIFIED")

    if not _valid_counts(post_inventory, len(post_positions), len(post_stops)):
        return block("SHARED_PROTECTED_POST_COMPLETENESS_OR_ORDERS_UNVERIFIED")
    return block(
        "SHARED_PROTECTED_SINGLE_ENTRY_OBSERVED_NONAUTHORIZING",
        preexisting_manual_positions=pre_witness.manual_positions_observed,
        preexisting_owned_positions=pre_witness.occupied_slots,
        total_positions_after=len(post_positions),
        new_t100_full_fill_and_native_stop_observed=True,
        legacy_manual_inventory_preserved=True,
    )


class SharedOutcomeVenue(Protocol):
    async def open_positions(self) -> list[dict[str, Any]]: ...
    async def open_stop_orders(self) -> list[dict[str, Any]]: ...
    async def order_by_external_oid(self, symbol: str, oid: str) -> dict[str, Any] | None: ...


class SharedOutcomeCompleteness(Protocol):
    async def inspect(self) -> dict[str, Any]: ...


async def observe_shared_protected_entry(
    *,
    pre_witness: FourSlotSnapshotResult,
    pre_positions: list[dict[str, Any]],
    pre_stops: list[dict[str, Any]],
    stage: DurableAttemptStage,
    intent: EntryIntent,
    venue: SharedOutcomeVenue,
    completeness: SharedOutcomeCompleteness,
) -> SharedProtectedEntryObservation:
    """Two independent full-account GET bookends and exact order identity GET.

    Neither a missing order nor a POST timeout authorizes a second submission.
    """
    def block(code: str) -> SharedProtectedEntryObservation:
        return SharedProtectedEntryObservation(status=code)
    if type(stage) is not DurableAttemptStage or type(intent) is not EntryIntent:
        return block("SHARED_PROTECTED_STAGE_OR_INTENT_INVALID")
    try:
        snapshots = []
        digests = []
        for _ in range(2):
            positions = await venue.open_positions()
            stops = await venue.open_stop_orders()
            order = await venue.order_by_external_oid(
                intent.symbol, intent.external_oid,
            )
            inventory = await completeness.inspect()
            if (
                type(positions) is not list
                or type(stops) is not list
                or type(order) is not dict
                or type(inventory) is not dict
            ):
                return block("SHARED_PROTECTED_ORDER_OR_GET_UNRESOLVED_NO_RETRY")
            digests.append((
                _venue_digest(positions, stops),
                _hash_rows({"orders": [order], "counts": [inventory]}),
            ))
            snapshots.append((positions, stops, order, inventory))
    except Exception:
        return block("SHARED_PROTECTED_ORDER_OR_GET_UNRESOLVED_NO_RETRY")
    if digests[0] != digests[1]:
        return block("SHARED_PROTECTED_POST_BOOKEND_DRIFT")
    positions, stops, order, inventory = snapshots[-1]
    return assess_shared_protected_entry(
        pre_witness=pre_witness,
        pre_positions=pre_positions, pre_stops=pre_stops,
        stage=stage, intent=intent,
        post_positions=positions, post_stops=stops,
        post_order=order, post_inventory=inventory,
    )
