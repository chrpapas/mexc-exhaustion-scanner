"""Order-free protected V40 fill -> durable-record preparation.

Prepares new order-fill sets and initial OPEN checkpoint version 1 only
after the previously implemented, account-wide four-slot protected-outcome
witness accepts every position and native stop. Does not write a database,
send MEXC calls or grant permission to place orders.

A prepared bundle is NOT a persisted ACK, a POST result, freshness proof,
exchange atomicity, liquidation certification, or restart certificate.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Sequence

from app.t100_live_checkpoint_journal import (
    CheckpointJournalHalted, ENTRY_PROTECTED, build_initial_checkpoint,
    validate_checkpoint_transition,
)
from app.t100_live_durable_recovery import PositionCheckpoint
from app.t100_live_execution import EntryIntent
from app.t100_live_fill_ledger import (
    VerifiedOrderFillSet, VerifiedFillLedgerHalted,
    build_verified_order_fill_set,
)
from app.t100_live_four_slot_attempt_journal import DurableAttemptStage
from app.t100_live_four_slot_attempt_outcome import assess_staged_protected_attempts
from app.t100_live_four_slot_snapshot_coordinator import FourSlotSnapshotResult


class ProtectedFinalizeHalted(RuntimeError):
    pass


@dataclass(frozen=True)
class PreparedProtectedEntry:
    stage: DurableAttemptStage
    intent: EntryIntent
    order_id: str
    position_id: str
    checkpoint: PositionCheckpoint
    fill_set: VerifiedOrderFillSet


@dataclass(frozen=True)
class PreparedProtectedBatch:
    entries: tuple[PreparedProtectedEntry, ...]
    status: str = "PROTECTED_BATCH_PREPARED_OFFLINE_ONLY"
    durable_ack_persisted: bool = False
    durable_checkpoint_persisted: bool = False
    durable_fill_set_persisted: bool = False
    native_stop_exchange_atomicity_verified: bool = False
    order_post_attempted: bool = False
    entry_allowed: bool = False
    live_authorization: bool = False
    mexc_orders_sent: bool = False
    read_only: bool = True


def prepare_protected_entries(
    *,
    stages: Sequence[DurableAttemptStage],
    intents: Sequence[EntryIntent],
    pre_attempt_witness: FourSlotSnapshotResult,
    orders: Sequence[dict[str, Any]],
    positions: Sequence[dict[str, Any]],
    stops: Sequence[dict[str, Any]],
    inventory: Any,
    deals_by_order_id: dict[str, list[dict[str, Any]]],
) -> PreparedProtectedBatch:
    """Fail closed unless every staged order has a complete, protected fill."""
    observation = assess_staged_protected_attempts(
        stages=stages, intents=intents,
        pre_attempt_witness=pre_attempt_witness,
        orders=orders, positions=positions, stops=stops,
        inventory=inventory,
    )
    if (
        observation.status
        != "ATTEMPT_ALL_NEW_POSITIONS_PROTECTED_OBSERVED_NONAUTHORIZING"
        or not observation.all_staged_attempts_fully_filled_and_protected
        or not observation.native_stop_witness_observed
        or type(deals_by_order_id) is not dict
    ):
        raise ProtectedFinalizeHalted("PROTECTED_OUTCOME_OR_DEALS_NOT_VERIFIED")
    by_oid = {order["externalOid"]: order for order in orders}
    order_ids = {str(order["orderId"]) for order in orders}
    if (
        len(by_oid) != len(orders)
        or len(order_ids) != len(orders)
        or set(deals_by_order_id) != order_ids
    ):
        raise ProtectedFinalizeHalted("FILL_ORDER_SET_NOT_EXACT")
    by_pos = {str(p["positionId"]): p for p in positions}
    by_stop = {str(s["positionId"]): s for s in stops}
    result = []
    try:
        for stage, intent in zip(stages, intents):
            order = by_oid[intent.external_oid]
            oid = str(order["orderId"])
            position_id = str(order["positionId"])
            fill_set = build_verified_order_fill_set(
                order_id=oid, order=order, fills=deals_by_order_id[oid],
                require_strategy_fully_filled=True,
            )
            if (
                fill_set.side != "OPEN_SHORT"
                or not fill_set.strategy_fully_filled_order_complete
                or fill_set.symbol != intent.symbol
                or fill_set.position_id != position_id
                or fill_set.deal_volume != intent.volume_contracts
                or not fill_set.fills
            ):
                raise ProtectedFinalizeHalted("FILL_SET_INTENT_IDENTITY_MISMATCH")
            # This is a proposed post-CAS ACK row, NOT a durable ACK.
            # The DB transaction must persist ACK with exact identity.
            hypothetical_ack = {
                "symbol": intent.symbol,
                "external_oid": intent.external_oid,
                "exchange_order_id": oid,
                "volume_contracts": intent.volume_contracts,
                "stop_price": intent.stop_price,
                "state": "ACK",
            }
            checkpoint = build_initial_checkpoint(
                intent=hypothetical_ack, order=order,
                position=by_pos[position_id], stop=by_stop[position_id],
            )
            validate_checkpoint_transition(
                None, checkpoint, reason=ENTRY_PROTECTED,
            )
            if (
                checkpoint.contracts != fill_set.deal_volume
                or checkpoint.entry_price != fill_set.average_price
                or checkpoint.position_id != fill_set.position_id
                or checkpoint.exchange_order_id != fill_set.order_id
            ):
                raise ProtectedFinalizeHalted("CHECKPOINT_FILL_SET_DRIFT")
            result.append(PreparedProtectedEntry(
                stage, intent, oid, position_id, checkpoint, fill_set,
            ))
    except (KeyError, ValueError, TypeError, CheckpointJournalHalted,
            VerifiedFillLedgerHalted) as exc:
        raise ProtectedFinalizeHalted(
            "PROTECTED_CHECKPOINT_OR_FILL_PROVENANCE_FAILED"
        ) from None
    if len({x.position_id for x in result}) != len(result):
        raise ProtectedFinalizeHalted("DUPLICATE_NEW_POSITION_ID")
    return PreparedProtectedBatch(entries=tuple(result))
