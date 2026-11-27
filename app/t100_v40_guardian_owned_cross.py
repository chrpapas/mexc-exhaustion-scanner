"""Read-only native MARKET stop audit for exactly one owned V40 CROSS SHORT.

The pre-entry V40 guardian must not stop observing when a legitimate first
position appears. This additional post-commit observer accepts a managed
position ONLY if immutable journal, independently reconciled signed GET
inventory, exact filled order origin and native full-volume MARKET SL agree.

An UNKNOWN or merely SUBMITTING order is NOT adopted. It is a critical
transition requiring operator attention rather than a random auto-close.
No API POST, account mutation or trading permit in this module.
"""
from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal as D
from typing import Any

from app.t100_live_four_slot_snapshot_coordinator import (
    FourSlotSnapshotResult, LedgerSnapshot, _ledger_digest,
)
from app.t100_live_durable_recovery import (
    DurableRecoveryHalted,checkpoint_from_row,
)
from app.t100_live_native_stop_execution_audit import (
    assess_native_short_market_stop_row,
)


@dataclass(frozen=True)
class V40OwnedCrossGuardianReport:
    status: str
    single_durable_owned_short: bool=False
    cross_1x_position_verified: bool=False
    native_market_sl_exact_order_and_quantity_verified: bool=False
    independent_venue_bookends_observed: bool=False
    account_wide_completeness_observed: bool=False
    active_positions: int=0
    active_stops: int=0
    incident_alert: bool=True
    live_stop_execution_certified: bool=False
    trailing_execution_certified: bool=False
    guardian_deployment_certified: bool=False
    entry_allowed: bool=False
    live_authorization: bool=False
    mexc_posts: int=0
    database_writes: int=0


def assess_v40_owned_cross_guardian(
    *,snapshot:FourSlotSnapshotResult,ledger:LedgerSnapshot,
    positions:Any,stops:Any,
) -> V40OwnedCrossGuardianReport:
    """Compare signed venue GETs with the identical recoverable V40 ledger.

    Never uses a provided 'owned=true' boolean to authorize or adopt an
    unrelated/manual position. Full-account completeness comes only from
    the existing signed MEXC current inventory probe+bookends. The native
    stop witness is still NOT proof that a future trigger will fill.
    """
    def halt(code:str)->V40OwnedCrossGuardianReport:
        return V40OwnedCrossGuardianReport(status=code)
    if (
        type(snapshot) is not FourSlotSnapshotResult
        or type(ledger) is not LedgerSnapshot
        or ledger.readonly_repeatable_read is not True
        or type(positions) is not list or type(stops) is not list
        or any(type(x) is not dict for x in positions+stops)
    ):
        return halt("V40_OWNED_CROSS_INPUT_OR_READONLY_SHAPE_INVALID_HALT")
    try:
        if _ledger_digest(ledger)!=snapshot.ledger_hash:
            return halt("V40_OWNED_CROSS_LEDGER_SNAPSHOT_CHANGED_HALT")
    except Exception:
        return halt("V40_OWNED_CROSS_LEDGER_HASH_UNVERIFIED_HALT")
    inventory=snapshot.inventory
    if (
        snapshot.status!="NONFLAT_PROVENANCE_MATCHED_NONAUTHORIZING"
        or snapshot.occupied_slots!=1
        or snapshot.recovery is None
        or len(snapshot.recovery.recovered)!=1
        or snapshot.recovery.checkpoint_count!=1
        or snapshot.recovery.exchange_position_count!=1
        or snapshot.recovery.exchange_stop_count!=1
        or type(inventory) is not dict
        or inventory.get("account_wide_completeness_verified") is not True
        or inventory.get("critical_inventory_equal_across_witness_window") is not True
        or len(ledger.checkpoints)!=1
        or len(positions)!=1 or len(stops)!=1
        or snapshot.entry_allowed is not False
        or snapshot.live_authorization is not False
    ):
        return halt("V40_OWNED_CROSS_NO_SINGLE_COMPLETE_OWNED_POSITION_HALT")
    try:
        cp=checkpoint_from_row(ledger.checkpoints[0])
    except (DurableRecoveryHalted,KeyError,TypeError,ValueError):
        return halt("V40_OWNED_CROSS_CHECKPOINT_IDENTITY_INVALID_HALT")
    pos=positions[0]
    stop=stops[0]
    matching=snapshot.recovery.recovered[0]
    if (
        cp.symbol!=matching.symbol
        or cp.position_id!=matching.position_id
        or cp.stop_order_id!=matching.stop_order_id
        or cp.contracts!=matching.contracts
        or cp.stop_price!=matching.stop_price
        or cp.lifecycle_state!="OPEN"
        or pos.get("symbol")!=cp.symbol
        or str(pos.get("positionId"))!=cp.position_id
        or type(pos.get("positionType")) is not int
        or pos["positionType"]!=2
        or type(pos.get("state")) is not int
        or pos["state"]!=1
        or type(pos.get("openType")) is not int
        or pos["openType"]!=2
        or type(pos.get("leverage")) is not int
        or pos["leverage"]!=1
        or type(pos.get("holdVol")) in (bool,type(None))
        or D(str(pos["holdVol"]))!=cp.contracts
        or str(stop.get("id") or stop.get("stopPlanOrderId"))
           !=cp.stop_order_id
    ):
        return halt("V40_OWNED_CROSS_POSITION_OR_CROSS_ONE_X_DRIFT_HALT")
    audit=assess_native_short_market_stop_row(
        stop=stop,symbol=cp.symbol,position_id=cp.position_id,
        position_quantity=cp.contracts,
        maximum_short_stop_price=cp.stop_price,
        verified_filled_entry_order_id=cp.exchange_order_id,
    )
    if audit.active_native_full_volume_market_sl_observed is not True:
        return halt("V40_OWNED_CROSS_NATIVE_MARKET_SL_UNVERIFIED_HALT")
    return V40OwnedCrossGuardianReport(
        status="V40_ONE_OWNED_CROSS_SHORT_NATIVE_MARKET_SL_OBSERVED_NONAUTHORIZING",
        single_durable_owned_short=True,
        cross_1x_position_verified=True,
        native_market_sl_exact_order_and_quantity_verified=True,
        independent_venue_bookends_observed=True,
        account_wide_completeness_observed=True,
        active_positions=1,active_stops=1,
        incident_alert=False,
    )
