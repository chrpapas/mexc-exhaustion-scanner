"""Read-only shared-account RESTART witness for one protected T100 V40 entry.

The provenance must come from a fresh, independently bookended account-wide
GET + repeatable-read SELECT snapshot after a hypothetical durable commit.
This module CANNOT prove that a DB transaction actually happened, infer that
manual positions are safe, certify cross-wallet collateral or grant order
authority. It compares exactly one new protected T100 position and preserves
every preexisting manual/owned position without requiring manual SL/TP.

No REST, SQL, Render, Postgres writes, leases, order POST, or retry.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from app.t100_live_four_slot_snapshot_coordinator import (
    FourSlotSnapshotResult, _valid_counts, _venue_digest,
)
from app.t100_live_shared_protected_entry_observer import (
    assess_shared_protected_entry,
)
from app.t100_live_v40_protected_finalization import (
    PreparedProtectedBatch,
)


@dataclass(frozen=True)
class SharedRestartProof:
    status: str
    recovered_t100_positions: int | None = None
    unchanged_manual_positions: int | None = None
    owned_native_stops_recovered: bool = False
    new_protected_fill_record_matches_recovery: bool = False
    historical_manual_positions_not_adopted: bool = True
    real_db_commit_independently_verified: bool = False
    production_restart_certified: bool = False
    native_stop_execution_certified: bool = False
    shared_cross_collateral_certified: bool = False
    entry_allowed: bool = False
    live_authorization: bool = False
    mexc_orders_sent: bool = False
    read_only: bool = True


def verify_shared_restart_protected_entry(
    *,
    pre_witness: FourSlotSnapshotResult,
    restart_witness: FourSlotSnapshotResult,
    prepared: PreparedProtectedBatch,
    pre_positions: Any,
    pre_stops: Any,
    restart_positions: Any,
    restart_stops: Any,
    origin_order: Any,
) -> SharedRestartProof:
    def blocked(status: str, **details: Any) -> SharedRestartProof:
        return SharedRestartProof(status=status, **details)

    if (
        type(pre_witness) is not FourSlotSnapshotResult
        or type(restart_witness) is not FourSlotSnapshotResult
        or type(prepared) is not PreparedProtectedBatch
        or len(prepared.entries) != 1
        or prepared.status != "PROTECTED_BATCH_PREPARED_OFFLINE_ONLY"
        or prepared.durable_ack_persisted is not False
        or prepared.durable_checkpoint_persisted is not False
        or prepared.durable_fill_set_persisted is not False
        or prepared.entry_allowed is not False
        or prepared.live_authorization is not False
        or prepared.mexc_orders_sent is not False
        or pre_witness.status not in {
            "SHARED_ACCOUNT_MANUAL_ONLY_NONAUTHORIZING",
            "SHARED_ACCOUNT_MANAGED_PROVENANCE_MATCHED_NONAUTHORIZING",
        }
        or restart_witness.status !=
            "SHARED_ACCOUNT_MANAGED_PROVENANCE_MATCHED_NONAUTHORIZING"
        or pre_witness.entry_allowed is not False
        or pre_witness.live_authorization is not False
        or restart_witness.entry_allowed is not False
        or restart_witness.live_authorization is not False
    ):
        return blocked("SHARED_RESTART_EVIDENCE_OR_MODE_UNVERIFIED")

    old_count = pre_witness.account_positions_observed
    old_owned = pre_witness.occupied_slots
    old_manual = pre_witness.manual_positions_observed
    after_count = restart_witness.account_positions_observed
    after_owned = restart_witness.occupied_slots
    after_manual = restart_witness.manual_positions_observed
    if (
        any(type(n) is not int for n in (
            old_count, old_owned, old_manual,
            after_count, after_owned, after_manual,
        ))
        or not 0 <= old_count < 4
        or old_count != old_owned + old_manual
        or after_count != old_count + 1
        or after_owned != old_owned + 1
        or after_manual != old_manual
        or not isinstance(pre_witness.critical_inventory_hash, str)
        or len(pre_witness.critical_inventory_hash) != 64
        or not isinstance(restart_witness.critical_inventory_hash, str)
        or len(restart_witness.critical_inventory_hash) != 64
        or not isinstance(pre_witness.ledger_hash, str)
        or len(pre_witness.ledger_hash) != 64
        or not isinstance(restart_witness.ledger_hash, str)
        or len(restart_witness.ledger_hash) != 64
        or type(pre_positions) is not list
        or type(pre_stops) is not list
        or type(restart_positions) is not list
        or type(restart_stops) is not list
        or not _valid_counts(
            pre_witness.inventory, old_count, len(pre_stops),
        )
        or not _valid_counts(
            restart_witness.inventory, after_count, len(restart_stops),
        )
    ):
        return blocked("SHARED_RESTART_SLOT_COUNTS_OR_COMPLETENESS_DRIFT")

    try:
        if (
            _venue_digest(pre_positions, pre_stops)
            != pre_witness.critical_inventory_hash
            or _venue_digest(restart_positions, restart_stops)
            != restart_witness.critical_inventory_hash
        ):
            return blocked("SHARED_RESTART_CRITICAL_BOOKEND_HASH_DRIFT")
    except (TypeError, ValueError, KeyError):
        return blocked("SHARED_RESTART_CRITICAL_ROWS_INVALID")

    entry = prepared.entries[0]
    if (
        entry.stage.entry_allowed is not False
        or entry.stage.live_authorization is not False
        or entry.intent.external_oid != entry.stage.external_oid
        or entry.checkpoint.external_oid != entry.intent.external_oid
        or entry.checkpoint.symbol != entry.intent.symbol
        or entry.checkpoint.position_id != entry.position_id
        or entry.checkpoint.exchange_order_id != entry.order_id
        or entry.fill_set.order_id != entry.order_id
        or entry.fill_set.position_id != entry.position_id
        or entry.fill_set.deal_volume != entry.checkpoint.contracts
        or entry.fill_set.average_price != entry.checkpoint.entry_price
        or entry.fill_set.final_order_fill_history_complete is not True
        or entry.fill_set.strategy_fully_filled_order_complete is not True
        or entry.checkpoint.lifecycle_state != "OPEN"
        or entry.checkpoint.checkpoint_version != 1
    ):
        return blocked("SHARED_RESTART_PREPARED_FILL_OR_CHECKPOINT_INVALID")

    # The original post-fill assessor also proves all unchanged manual
    # positions and their TP/SL rows, position side/mode, and correct newly
    # observed SL75 full-volume stop. Exact order ID must remain unchanged.
    original = assess_shared_protected_entry(
        pre_witness=pre_witness,
        pre_positions=pre_positions,
        pre_stops=pre_stops,
        stage=entry.stage,
        intent=entry.intent,
        post_positions=restart_positions,
        post_stops=restart_stops,
        post_order=origin_order,
        post_inventory=restart_witness.inventory,
    )
    if (
        original.status != "SHARED_PROTECTED_SINGLE_ENTRY_OBSERVED_NONAUTHORIZING"
        or original.legacy_manual_inventory_preserved is not True
        or original.new_t100_full_fill_and_native_stop_observed is not True
    ):
        return blocked("SHARED_RESTART_EXCHANGE_OR_MANUAL_INVENTORY_DRIFT")

    recovery = restart_witness.recovery
    if (
        recovery is None
        or recovery.mode != "SHADOW_ONLY_RECOVERED_NONFLAT"
        or recovery.live_authorization is not False
        or type(recovery.recovered) is not tuple
        or len(recovery.recovered) != after_owned
        or recovery.checkpoint_count != after_owned
        or recovery.exchange_position_count != after_owned
        or recovery.exchange_stop_count != after_owned
    ):
        return blocked("SHARED_RESTART_DURABLE_RECOVERY_UNVERIFIED")
    recovered = {p.position_id: p for p in recovery.recovered}
    target = recovered.get(entry.position_id)
    if (
        len(recovered) != after_owned
        or target is None
        or target.symbol != entry.checkpoint.symbol
        or target.contracts != entry.checkpoint.contracts
        or target.entry_price != entry.checkpoint.entry_price
        or target.stop_order_id != entry.checkpoint.stop_order_id
        or target.stop_price != entry.checkpoint.stop_price
        or target.best_profit_pct != entry.checkpoint.best_profit_pct
        or target.armed != entry.checkpoint.armed
    ):
        return blocked("SHARED_RESTART_T100_CHECKPOINT_OR_STOP_MISMATCH")

    if pre_witness.recovery is not None:
        before_owned_ids = {p.position_id for p in pre_witness.recovery.recovered}
        if (
            len(before_owned_ids) != old_owned
            or not before_owned_ids.isdisjoint({entry.position_id})
            or before_owned_ids != (set(recovered) - {entry.position_id})
            or any(
                recovered[p.position_id] != p
                for p in pre_witness.recovery.recovered
            )
        ):
            return blocked("SHARED_RESTART_OLD_T100_PROVENANCE_DRIFT")
    elif old_owned:
        return blocked("SHARED_RESTART_MISSING_PREVIOUS_T100_RECOVERY")

    return blocked(
        "SHARED_RESTART_EXACT_PROTECTED_PROVENANCE_OBSERVED_NONAUTHORIZING",
        recovered_t100_positions=after_owned,
        unchanged_manual_positions=after_manual,
        owned_native_stops_recovered=True,
        new_protected_fill_record_matches_recovery=True,
    )
