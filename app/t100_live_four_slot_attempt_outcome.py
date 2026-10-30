"""Read-only exchange outcome witness for 0-4 staged V40 short attempts.

Connects the EXISTING four-slot attempt journal's SUBMITTING identities to
observable order/fill/position/native-stop rows. Accounts for independently
recovered previously owned positions; any extra/manual position blocks.

No order submission, stop modification, database write, GET transport,
live permit, recovery checkpoint write, or environment switch here.
A successful result is only an observed protected candidate, NOT a durable
ACK, full exchange atomicity, verified margin, or live authorization.
"""
from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from typing import Any, Sequence, Protocol

from app.t100_live_four_slot_snapshot_coordinator import (
    _venue_digest, _hash_rows,
)

from app.t100_live_active_tpsl import effective_protective_quantity
from app.t100_live_execution import EntryIntent
from app.t100_live_four_slot_attempt_journal import DurableAttemptStage
from app.t100_live_four_slot_snapshot_coordinator import FourSlotSnapshotResult

D = Decimal

@dataclass(frozen=True)
class ProtectedAttemptObservation:
    status: str
    staged_attempts: int = 0
    preexisting_owned_positions: int = 0
    observed_positions: int = 0
    observed_current_stops: int = 0
    newly_observed_protected_positions: int = 0
    all_staged_attempts_fully_filled_and_protected: bool = False
    native_stop_witness_observed: bool = False
    account_wide_consistency_claimed: bool = False
    durable_ack_persisted: bool = False
    durable_checkpoint_persisted: bool = False
    private_collateral_verified: bool = False
    execution_capacity_verified: bool = False
    exchange_atomicity_verified: bool = False
    entry_allowed: bool = False
    live_authorization: bool = False
    mexc_orders_sent: bool = False
    read_only: bool = True


def _pos(value: Any) -> D:
    if value is None or type(value) is bool:
        raise ValueError("INVALID_DECIMAL")
    try:
        result = D(str(value))
    except (InvalidOperation, ValueError, TypeError):
        raise ValueError("INVALID_DECIMAL") from None
    if not result.is_finite() or result <= 0:
        raise ValueError("INVALID_DECIMAL")
    return result


def _identity(value: Any) -> str:
    if value is None or type(value) is bool:
        raise ValueError("INVALID_ID")
    s = str(value)
    if not s or s in {"0", "None"}:
        raise ValueError("INVALID_ID")
    return s


def assess_staged_protected_attempts(
    *, stages: Sequence[DurableAttemptStage],
    intents: Sequence[EntryIntent],
    pre_attempt_witness: FourSlotSnapshotResult,
    orders: Sequence[dict[str, Any]],
    positions: Sequence[dict[str, Any]],
    stops: Sequence[dict[str, Any]],
    inventory: Any,
) -> ProtectedAttemptObservation:
    def block(reason: str, **kw: Any) -> ProtectedAttemptObservation:
        return ProtectedAttemptObservation(status=reason, **kw)

    if (
        type(pre_attempt_witness) is not FourSlotSnapshotResult
        or pre_attempt_witness.status not in {
            "FLAT_OBSERVED_NONAUTHORIZING",
            "NONFLAT_PROVENANCE_MATCHED_NONAUTHORIZING",
        }
        or pre_attempt_witness.entry_allowed is not False
        or pre_attempt_witness.live_authorization is not False
        or type(pre_attempt_witness.occupied_slots) is not int
        or not 0 <= pre_attempt_witness.occupied_slots < 4
    ):
        return block("ATTEMPT_PREEXISTING_OWNERSHIP_UNVERIFIED")
    old = () if pre_attempt_witness.recovery is None else pre_attempt_witness.recovery.recovered
    if (
        (pre_attempt_witness.recovery is not None and
         pre_attempt_witness.recovery.live_authorization is not False)
        or (pre_attempt_witness.recovery is not None and
            pre_attempt_witness.recovery.mode != "SHADOW_ONLY_RECOVERED_NONFLAT")
        or not isinstance(pre_attempt_witness.ledger_hash, str)
        or len(pre_attempt_witness.ledger_hash) != 64
        or not isinstance(pre_attempt_witness.critical_inventory_hash, str)
        or len(pre_attempt_witness.critical_inventory_hash) != 64
        or len(old) != pre_attempt_witness.occupied_slots
        or (bool(old) != (pre_attempt_witness.status == "NONFLAT_PROVENANCE_MATCHED_NONAUTHORIZING"))
    ):
        return block("ATTEMPT_PREEXISTING_RECOVERY_UNVERIFIED")
    if (
        not isinstance(stages, (tuple, list))
        or not isinstance(intents, (tuple, list))
        or not isinstance(orders, (tuple, list))
        or not isinstance(positions, (tuple, list))
        or not isinstance(stops, (tuple, list))
        or any(type(item) is not dict for item in (*orders, *positions, *stops))
        or not 1 <= len(stages) == len(intents) == len(orders) <= 4 - len(old)
        or len(positions) > 4
    ):
        return block("ATTEMPT_COLLECTIONS_OR_SLOT_COUNT_INVALID")
    common = dict(
        staged_attempts=len(stages),
        preexisting_owned_positions=len(old),
        observed_positions=len(positions),
        observed_current_stops=len(stops),
    )
    seen_oids: set[str] = set()
    seen_sids: set[int] = set()
    expected_new_symbols: set[str] = set()
    for stage, intent in zip(stages, intents):
        if (
            type(stage) is not DurableAttemptStage
            or type(intent) is not EntryIntent
            or stage.state != "SUBMITTING"
            or stage.post_attempted is not False
            or stage.reconciliation_required is not True
            or stage.entry_allowed is not False
            or stage.live_authorization is not False
            or type(stage.signal_id) is not int or stage.signal_id <= 0
            or stage.signal_id != intent.signal_id
            or stage.external_oid != intent.external_oid
            or not intent.external_oid
            or intent.external_oid in seen_oids
            or stage.signal_id in seen_sids
            or intent.symbol in expected_new_symbols
            or intent.symbol in {p.symbol for p in old}
        ):
            return block("ATTEMPT_STAGE_IDENTITY_OR_OWNERSHIP_INVALID", **common)
        seen_oids.add(intent.external_oid)
        seen_sids.add(stage.signal_id)
        expected_new_symbols.add(intent.symbol)

    if type(inventory) is not dict:
        return block("ATTEMPT_ACCOUNT_COMPLETENESS_MISSING", **common)
    counts = inventory.get("open_order_counts")
    if (
        inventory.get("account_wide_completeness_verified") is not True
        or inventory.get("critical_inventory_equal_across_witness_window") is not True
        or any(type(inventory.get(k)) is not int for k in (
            "before_positions", "after_positions",
            "before_current_tpsl", "after_current_tpsl",
        ))
        or any(inventory[k] != n for k, n in (
            ("before_positions", len(positions)),
            ("after_positions", len(positions)),
            ("before_current_tpsl", len(stops)),
            ("after_current_tpsl", len(stops)),
        ))
        or type(counts) is not dict
        or any(type(counts.get(k)) is not int or counts[k] < 0 for k in (
            "sumCount", "limitOrderCount", "stopOrderCount",
            "planOrderCount", "trackOrderCount",
        ))
        or counts["stopOrderCount"] != len(stops)
        or counts["sumCount"] != sum(counts[k] for k in (
            "limitOrderCount", "stopOrderCount", "planOrderCount",
            "trackOrderCount",
        ))
        or any(counts[k] for k in (
            "limitOrderCount", "planOrderCount", "trackOrderCount",
        ))
    ):
        return block("ATTEMPT_ACCOUNT_COMPLETENESS_OR_OPEN_ORDERS_BLOCK", **common)

    try:
        pos_map: dict[str, dict[str, Any]] = {}
        stop_map: dict[str, dict[str, Any]] = {}
        oid_map: dict[str, dict[str, Any]] = {}
        for position in positions:
            pid = _identity(position.get("positionId"))
            if pid in pos_map:
                raise ValueError("DUPLICATE_POS")
            pos_map[pid] = position
        for stop in stops:
            pid = _identity(stop.get("positionId"))
            if pid in stop_map or _identity(stop.get("id")) in {
                _identity(row.get("id")) for row in stop_map.values()
            }:
                raise ValueError("DUPLICATE_STOP")
            stop_map[pid] = stop
        for order in orders:
            oid = _identity(order.get("externalOid"))
            if oid in oid_map:
                raise ValueError("DUPLICATE_ORDER")
            oid_map[oid] = order

        if len(pos_map) != len(old) + len(stages):
            return block("ATTEMPT_ACCOUNT_UNOWNED_OR_MISSING_POSITION", **common)
        if len(stop_map) != len(pos_map):
            return block("ATTEMPT_ACCOUNT_STOP_COUNT_MISMATCH", **common)
        if set(oid_map) != seen_oids:
            return block("ATTEMPT_ORDER_EXTERNAL_OID_SET_MISMATCH", **common)
        for previous in old:
            pid = _identity(previous.position_id)
            p, s = pos_map.get(pid), stop_map.get(pid)
            if p is None or s is None or p.get("symbol") != previous.symbol or s.get("symbol") != previous.symbol:
                return block("ATTEMPT_PREEXISTING_POSITION_NOT_PRESERVED", **common)
            if (
                _pos(p.get("holdVol")) != previous.contracts
                or _pos(p.get("holdAvgPrice")) != previous.entry_price
                or _pos(s.get("stopLossPrice")) != previous.stop_price
                or _identity(s.get("id")) != previous.stop_order_id
                or p.get("positionType") != 2 or p.get("state") != 1
                or s.get("state") != 1 or s.get("isFinished") != 0
                or s.get("positionType") != 2
                or effective_protective_quantity(s, position_quantity=previous.contracts) != previous.contracts
            ):
                return block("ATTEMPT_PREEXISTING_POSITION_CHANGED", **common)

        for intent in intents:
            order = oid_map[intent.external_oid]
            if (
                order.get("symbol") != intent.symbol
                or order.get("side") != 3
                or order.get("state") != 3
                or _identity(order.get("orderId")) in {
                    _identity(o.get("orderId")) for o in orders
                    if o is not order
                }
            ):
                return block("ATTEMPT_ORDER_NOT_FINAL_SHORT_OR_DUPLICATE", **common)
            deal = _pos(order.get("dealVol"))
            if deal != _pos(intent.volume_contracts):
                return block("ATTEMPT_PARTIAL_OR_MISMATCHED_FILL_BLOCK", **common)
            pid = _identity(order.get("positionId"))
            p, s = pos_map.get(pid), stop_map.get(pid)
            if p is None or s is None:
                return block("ATTEMPT_FILLED_POSITION_OR_STOP_MISSING", **common)
            if (
                p.get("symbol") != intent.symbol or s.get("symbol") != intent.symbol
                or p.get("positionType") != 2 or p.get("openType") != 2
                or p.get("state") != 1 or s.get("state") != 1
                or s.get("positionType") != 2 or s.get("isFinished") != 0
                or _pos(p.get("holdVol")) != deal
                or _pos(p.get("holdAvgPrice")) != _pos(order.get("dealAvgPrice"))
                or _pos(s.get("stopLossPrice")) > _pos(intent.stop_price)
                or effective_protective_quantity(s, position_quantity=deal) != deal
            ):
                return block("ATTEMPT_NATIVE_STOP_OR_POSITION_UNVERIFIED", **common)
        if {p.symbol for p in old} | expected_new_symbols != {
            p.get("symbol") for p in positions
        }:
            return block("ATTEMPT_UNOWNED_SYMBOL_IN_ACCOUNT", **common)
    except (TypeError, ValueError, ArithmeticError, InvalidOperation, KeyError):
        return block("ATTEMPT_EXCHANGE_ROWS_MALFORMED", **common)

    return block(
        "ATTEMPT_ALL_NEW_POSITIONS_PROTECTED_OBSERVED_NONAUTHORIZING",
        newly_observed_protected_positions=len(stages),
        all_staged_attempts_fully_filled_and_protected=True,
        native_stop_witness_observed=True,
        **common,
    )


class AttemptVenueReader(Protocol):
    async def open_positions(self) -> list[dict[str, Any]]: ...
    async def open_stop_orders(self) -> list[dict[str, Any]]: ...
    async def order_by_external_oid(
        self, symbol: str, external_oid: str,
    ) -> dict[str, Any] | None: ...


class AttemptInventoryReader(Protocol):
    async def inspect(self) -> dict[str, Any]: ...


async def observe_staged_protected_attempts(
    *, stages: Sequence[DurableAttemptStage],
    intents: Sequence[EntryIntent],
    pre_attempt_witness: FourSlotSnapshotResult,
    venue: AttemptVenueReader,
    completeness: AttemptInventoryReader,
) -> ProtectedAttemptObservation:
    """Two GET-only bookends; detect current critical drift and ambiguous fills.

    This is NOT an atomic venue transaction. In particular, ABA changes or
    between-read events remain possible. The DB ledger remains unmodified.
    """
    def blocked(reason: str) -> ProtectedAttemptObservation:
        return ProtectedAttemptObservation(status=reason)

    if (
        not isinstance(stages, (tuple, list))
        or not isinstance(intents, (tuple, list))
        or not 1 <= len(stages) == len(intents) <= 4
        or any(type(s) is not DurableAttemptStage for s in stages)
        or any(type(i) is not EntryIntent for i in intents)
    ):
        return blocked("ATTEMPT_STAGE_ARGS_UNVERIFIED")
    try:
        reads = []
        fingerprints = []
        for _ in range(2):
            positions = await venue.open_positions()
            stops = await venue.open_stop_orders()
            if type(positions) is not list or type(stops) is not list:
                return blocked("ATTEMPT_GET_ROWS_MALFORMED")
            orders = []
            for intent in intents:
                order = await venue.order_by_external_oid(
                    intent.symbol, intent.external_oid,
                )
                if type(order) is not dict:
                    return blocked("ATTEMPT_EXACT_ORDER_GET_UNRESOLVED_NO_RETRY")
                orders.append(order)
            report = await completeness.inspect()
            if type(report) is not dict:
                return blocked("ATTEMPT_COMPLETENESS_READ_INVALID")
            fingerprints.append((
                _venue_digest(positions, stops),
                _hash_rows({"orders": orders, "completeness": [report]}),
            ))
            reads.append((positions, stops, orders, report))
    except Exception:
        return blocked("ATTEMPT_GET_OR_FINGERPRINT_FAILED")
    if fingerprints[0] != fingerprints[1]:
        return blocked("ATTEMPT_BOOKEND_CRITICAL_DRIFT_OR_ORDER_CHANGE")
    positions, stops, orders, report = reads[-1]
    return assess_staged_protected_attempts(
        stages=stages, intents=intents,
        pre_attempt_witness=pre_attempt_witness,
        orders=orders, positions=positions, stops=stops,
        inventory=report,
    )
