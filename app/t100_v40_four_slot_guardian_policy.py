"""Pure 1–4 slot T100/V40 SHORT CROSS/native MARKET SL guardian audit.

This is a READ-ONLY bridge from the existing full-account signed GET,
REPEATABLE READ ledger and durable V40 recovery. It does NOT certify the
exchange leverage or tier, cross collateral, liquidation distance, fills
or native stop execution; never yields a live order permit or trades.
A single unrelated position, missing stop, stale ledger or duplicate
position/stop identity fails the entire account. Uses per-position ids,
NOT list order; all four positions must have distinct symbols.

Do not import from the current one-shot Render guardian until its full
multi-slot account scope, tier verification and recovery protocol are
independently reviewed and deployed.
"""
from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal as D, InvalidOperation
from typing import Any

from app.t100_live_durable_recovery import (
    DurableRecoveryHalted, checkpoint_from_row,
)
from app.t100_live_four_slot_snapshot_coordinator import (
    FourSlotSnapshotResult, LedgerSnapshot, _ledger_digest, _venue_digest,
)
from app.t100_live_native_stop_execution_audit import (
    assess_native_short_market_stop_row,
)


@dataclass(frozen=True)
class V40FourSlotGuardianAudit:
    status: str
    protected_owned_positions: int = 0
    matching_native_market_stops: int = 0
    separate_symbols_verified: bool = False
    account_bookends_verified: bool = False
    ledger_bookends_verified: bool = False
    all_owned_cross_positions_verified: bool = False
    native_stop_execution_certified: bool = False
    exchange_margin_and_liquidation_certified: bool = False
    entry_allowed: bool = False
    live_authorization: bool = False
    mexc_posts: int = 0
    database_writes: int = 0
    incident_alert: bool = True


def audit_frozen_four_slot_native_stops(
    *,
    snapshot: FourSlotSnapshotResult,
    ledger: LedgerSnapshot,
    account_positions: list[dict[str, Any]],
    account_stops: list[dict[str, Any]],
) -> V40FourSlotGuardianAudit:
    """Only reads matched *signed* source results from existing coordinator.

    A positive result is a candidate for further independent guardian
    tests, NOT a release or proof a future stop will execute.
    """
    halt=lambda code:V40FourSlotGuardianAudit(status=code)
    if (
        type(snapshot) is not FourSlotSnapshotResult
        or type(ledger) is not LedgerSnapshot
        or ledger.readonly_repeatable_read is not True
        or type(account_positions) is not list
        or type(account_stops) is not list
        or any(type(x) is not dict for x in account_positions+account_stops)
    ):
        return halt("V40_FOUR_SLOT_GUARDIAN_SOURCE_INVALID")
    try:
        if _ledger_digest(ledger)!=snapshot.ledger_hash:
            return halt("V40_FOUR_SLOT_GUARDIAN_LEDGER_DRIFT")
    except Exception:
        return halt("V40_FOUR_SLOT_GUARDIAN_LEDGER_UNVERIFIED")
    # The candidate position/stop arrays MUST be exactly the same signed
    # venue bookend that the upstream coordinator audited. A caller must
    # never substitute newer/different rows after the verified snapshot.
    try:
        if _venue_digest(account_positions,account_stops)!=snapshot.critical_inventory_hash:
            return halt("V40_FOUR_SLOT_GUARDIAN_VENUE_BOOKEND_DRIFT")
    except Exception:
        return halt("V40_FOUR_SLOT_GUARDIAN_VENUE_DIGEST_UNVERIFIED")
    inv=snapshot.inventory
    if not (
        snapshot.status=="NONFLAT_PROVENANCE_MATCHED_NONAUTHORIZING"
        and snapshot.entry_allowed is False
        and snapshot.live_authorization is False
        and 1 <= snapshot.occupied_slots <=4
        and snapshot.recovery is not None
        and type(inv) is dict
        and inv.get("account_wide_completeness_verified") is True
        and inv.get("critical_inventory_equal_across_witness_window") is True
        and inv.get("before_positions")==snapshot.occupied_slots
        and inv.get("after_positions")==snapshot.occupied_slots
        and inv.get("before_current_tpsl")==snapshot.occupied_slots
        and inv.get("after_current_tpsl")==snapshot.occupied_slots
        and len(account_positions)==snapshot.occupied_slots
        and len(account_stops)==snapshot.occupied_slots
        and len(ledger.checkpoints)==snapshot.occupied_slots
        and len(snapshot.recovery.recovered)==snapshot.occupied_slots
        and snapshot.recovery.checkpoint_count==snapshot.occupied_slots
        and snapshot.recovery.exchange_position_count==snapshot.occupied_slots
        and snapshot.recovery.exchange_stop_count==snapshot.occupied_slots
    ):
        return halt("V40_FOUR_SLOT_GUARDIAN_ACCOUNT_OR_RECOVERY_INCOMPLETE")
    try:
        check=[checkpoint_from_row(r) for r in ledger.checkpoints]
    except (DurableRecoveryHalted, KeyError,TypeError,ValueError):
        return halt("V40_FOUR_SLOT_GUARDIAN_CHECKPOINT_MALFORMED")
    by_position={str(p.get("positionId")):p for p in account_positions}
    by_stop_position={str(p.get("positionId")):p for p in account_stops}
    cp_ids={c.position_id for c in check}
    recovered_ids={c.position_id for c in snapshot.recovery.recovered}
    stopids={c.stop_order_id for c in check}
    symbols={c.symbol for c in check}
    if not (
        len(cp_ids)==len(by_position)==len(by_stop_position)==len(stopids)
        ==len(symbols)==len(check)
        and cp_ids==set(by_position)==set(by_stop_position)==recovered_ids
        and all(c.lifecycle_state=="OPEN" for c in check)
    ):
        return halt("V40_FOUR_SLOT_GUARDIAN_UNOWNED_OR_DUPLICATE_POSITION")
    recovered_by_id={r.position_id:r for r in snapshot.recovery.recovered}
    for cp in check:
        pos=by_position[cp.position_id]
        stop=by_stop_position[cp.position_id]
        rec=recovered_by_id[cp.position_id]
        try:
            qty=D(str(pos.get("holdVol")))
            lev=pos.get("leverage")
        except (TypeError,InvalidOperation,ValueError):
            return halt("V40_FOUR_SLOT_GUARDIAN_POSITION_NUMBER_INVALID")
        if not (
            cp.position_id==rec.position_id
            and cp.symbol==pos.get("symbol")==rec.symbol
            and cp.stop_order_id==rec.stop_order_id
            and cp.stop_price==rec.stop_price
            and cp.contracts==rec.contracts==qty
            and type(pos.get("positionType")) is int
            and pos["positionType"]==2
            and type(pos.get("openType")) is int
            and pos["openType"]==2
            and type(pos.get("state")) is int
            and pos["state"]==1
            and type(lev) is int and 1 <= lev <= 125
            and qty.is_finite() and qty>0
            and str(stop.get("id") or stop.get("stopPlanOrderId"))==cp.stop_order_id
        ):
            return halt("V40_FOUR_SLOT_GUARDIAN_SHORT_CROSS_OR_STOP_DRIFT")
        audit=assess_native_short_market_stop_row(
            stop=stop,symbol=cp.symbol,position_id=cp.position_id,
            position_quantity=cp.contracts,
            maximum_short_stop_price=cp.stop_price,
            verified_filled_entry_order_id=cp.exchange_order_id,
        )
        if audit.active_native_full_volume_market_sl_observed is not True:
            return halt("V40_FOUR_SLOT_GUARDIAN_NATIVE_MARKET_SL_UNVERIFIED")
    return V40FourSlotGuardianAudit(
        status="V40_FOUR_SLOT_ACCOUNT_NATIVE_MARKET_SL_SIGNED_GET_NONAUTHORIZING",
        protected_owned_positions=len(check),
        matching_native_market_stops=len(check),
        separate_symbols_verified=True,
        account_bookends_verified=True,
        ledger_bookends_verified=True,
        all_owned_cross_positions_verified=True,
        incident_alert=False,
    )
