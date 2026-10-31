"""Order-free preparation of a shared-account V40 protected fill/checkpoint.

Builds exact immutable fill-set and candidate version-1 OPEN checkpoint from
a TWO-BOOKEND-ELIGIBLE read-only outcome, but persists NOTHING. Caller must
independently supply contemporaneous full-account GETs, exact order details
and complete deal history. This does not authorize a POST or acknowledge a
ledger intent. A later production transaction must independently recheck
leader fencing, all-account inventory, unresolved mutations and ownership.
"""
from __future__ import annotations

from typing import Any

from app.t100_live_checkpoint_journal import (
    CheckpointJournalHalted, ENTRY_PROTECTED,
    build_initial_checkpoint, validate_checkpoint_transition,
)
from app.t100_live_execution import EntryIntent
from app.t100_live_fill_ledger import (
    VerifiedFillLedgerHalted, build_verified_order_fill_set,
)
from app.t100_live_four_slot_attempt_journal import DurableAttemptStage
from app.t100_live_four_slot_snapshot_coordinator import FourSlotSnapshotResult
from app.t100_live_shared_protected_entry_observer import assess_shared_protected_entry
from app.t100_live_v40_protected_finalization import (
    PreparedProtectedBatch, PreparedProtectedEntry, ProtectedFinalizeHalted,
)


def prepare_shared_protected_entry_offline(
    *,
    pre_witness: FourSlotSnapshotResult,
    pre_positions: list[dict[str, Any]],
    pre_stops: list[dict[str, Any]],
    stage: DurableAttemptStage,
    intent: EntryIntent,
    post_positions: list[dict[str, Any]],
    post_stops: list[dict[str, Any]],
    post_order: dict[str, Any],
    post_inventory: dict[str, Any],
    order_deals: list[dict[str, Any]],
) -> PreparedProtectedBatch:
    """No IO. Requires all-account protected outcome plus exact fills.

    The GET-only observer must already have checked two full-bookend reads.
    This function cannot independently prove that the supplied rows came
    from a fresh/current real exchange observation.
    """
    proof = assess_shared_protected_entry(
        pre_witness=pre_witness,
        pre_positions=pre_positions,
        pre_stops=pre_stops,
        stage=stage, intent=intent,
        post_positions=post_positions,
        post_stops=post_stops,
        post_order=post_order,
        post_inventory=post_inventory,
    )
    if (
        proof.status != "SHARED_PROTECTED_SINGLE_ENTRY_OBSERVED_NONAUTHORIZING"
        or proof.new_t100_full_fill_and_native_stop_observed is not True
        or proof.legacy_manual_inventory_preserved is not True
        or type(order_deals) is not list
        or type(post_order) is not dict
    ):
        raise ProtectedFinalizeHalted("SHARED_PROTECTED_OUTCOME_OR_DEALS_INVALID")

    try:
        order_id = str(post_order["orderId"])
        position_id = str(post_order["positionId"])
        position = next(
            p for p in post_positions if str(p.get("positionId")) == position_id
        )
        stop = next(
            s for s in post_stops if str(s.get("positionId")) == position_id
        )
        # Verify fees, trade IDs, full exact volume, price, side, and no
        # duplicate/deal-history discrepancy before constructing a candidate.
        fill_set = build_verified_order_fill_set(
            order_id=order_id, order=post_order,
            fills=order_deals, require_strategy_fully_filled=True,
        )
        if (
            fill_set.side != "OPEN_SHORT"
            or fill_set.symbol != intent.symbol
            or fill_set.position_id != position_id
            or fill_set.order_id != order_id
            or fill_set.deal_volume != intent.volume_contracts
            or not fill_set.fills
            or fill_set.strategy_fully_filled_order_complete is not True
        ):
            raise ProtectedFinalizeHalted("SHARED_FILL_SET_IDENTITY_MISMATCH")
        proposed_ack = {
            "symbol": intent.symbol,
            "external_oid": intent.external_oid,
            "exchange_order_id": order_id,
            "volume_contracts": intent.volume_contracts,
            "stop_price": intent.stop_price,
            "state": "ACK",
        }
        checkpoint = build_initial_checkpoint(
            intent=proposed_ack, order=post_order,
            position=position, stop=stop,
        )
        validate_checkpoint_transition(
            None, checkpoint, reason=ENTRY_PROTECTED,
        )
        if (
            checkpoint.position_id != fill_set.position_id
            or checkpoint.exchange_order_id != fill_set.order_id
            or checkpoint.contracts != fill_set.deal_volume
            or checkpoint.entry_price != fill_set.average_price
            or checkpoint.symbol != fill_set.symbol
        ):
            raise ProtectedFinalizeHalted("SHARED_CHECKPOINT_FILL_SET_DRIFT")
    except (KeyError, StopIteration, TypeError, ValueError,
            VerifiedFillLedgerHalted, CheckpointJournalHalted):
        raise ProtectedFinalizeHalted(
            "SHARED_PROTECTED_FILL_OR_CHECKPOINT_NOT_VERIFIED"
        ) from None

    return PreparedProtectedBatch(entries=(
        PreparedProtectedEntry(
            stage=stage, intent=intent, order_id=order_id,
            position_id=position_id, checkpoint=checkpoint,
            fill_set=fill_set,
        ),
    ))
