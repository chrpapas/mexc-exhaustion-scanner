"""Nonauthorizing GET-only bridge from four-slot staged attempts to fill/checkpoint preparation.

This module does not transport orders, persist ACKs, mutate native stops, access
credentials, grant execution permission, or certify an atomic MEXC snapshot.
Only an already independently staged SUBMITTING intent can be examined.
An unknown outcome NEVER permits order resubmission.
"""
from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass
from typing import Any, Protocol, Sequence

from app.t100_live_execution import EntryIntent
from app.t100_live_four_slot_attempt_journal import DurableAttemptStage
from app.t100_live_four_slot_snapshot_coordinator import (
    FourSlotSnapshotResult, _hash_rows, _venue_digest,
)
from app.t100_live_v40_protected_finalization import (
    PreparedProtectedBatch, ProtectedFinalizeHalted, prepare_protected_entries,
)


@dataclass(frozen=True)
class ProtectedGetPreparation:
    status: str
    prepared_batch: PreparedProtectedBatch | None = None
    orders_observed: int = 0
    deal_histories_observed: int = 0
    two_bookends_equal: bool = False
    durable_ack_persisted: bool = False
    durable_checkpoint_persisted: bool = False
    exchange_atomicity_verified: bool = False
    entry_allowed: bool = False
    live_authorization: bool = False
    mexc_orders_sent: bool = False
    read_only: bool = True


class ProtectedGetVenue(Protocol):
    async def open_positions(self) -> list[dict[str, Any]]: ...
    async def open_stop_orders(self) -> list[dict[str, Any]]: ...
    async def order_by_external_oid(
        self, symbol: str, external_oid: str,
    ) -> dict[str, Any] | None: ...
    async def order_deal_details(
        self, order_id: str,
    ) -> list[dict[str, Any]]: ...


class ProtectedGetCompleteness(Protocol):
    async def inspect(self) -> dict[str, Any]: ...


async def observe_and_prepare_staged_protection_get_only(
    *, stages: Sequence[DurableAttemptStage],
    intents: Sequence[EntryIntent],
    pre_attempt_witness: FourSlotSnapshotResult,
    venue: ProtectedGetVenue,
    completeness: ProtectedGetCompleteness,
) -> ProtectedGetPreparation:
    """Read full-account/order/deals twice and create only an offline bundle.

    Does not commit the bundle. Every invocation is nonauthorizing and
    read-only even when both snapshots match. A second complete GET set
    protects against observed drift, not exchange ABA or atomicity.
    """
    def block(code: str) -> ProtectedGetPreparation:
        return ProtectedGetPreparation(status=code)

    if (
        not isinstance(stages, (list, tuple))
        or not isinstance(intents, (list, tuple))
        or not 1 <= len(stages) == len(intents) <= 4
        or any(type(stage) is not DurableAttemptStage for stage in stages)
        or any(type(intent) is not EntryIntent for intent in intents)
        or len({stage.external_oid for stage in stages}) != len(stages)
        or len({intent.external_oid for intent in intents}) != len(intents)
        or any(
            stage.signal_id != intent.signal_id
            or stage.external_oid != intent.external_oid
            or stage.state != "SUBMITTING"
            or stage.post_attempted is not False
            or stage.reconciliation_required is not True
            or stage.entry_allowed is not False
            or stage.live_authorization is not False
            for stage, intent in zip(stages, intents)
        )
        or type(pre_attempt_witness) is not FourSlotSnapshotResult
        or pre_attempt_witness.entry_allowed is not False
        or pre_attempt_witness.live_authorization is not False
    ):
        return block("PROTECTED_GET_STAGED_IDENTITY_UNVERIFIED")

    snapshots: list[tuple[Any, Any, Any, Any, Any]] = []
    fingerprints: list[tuple[str, str]] = []
    try:
        for _ in range(2):
            positions = await venue.open_positions()
            stops = await venue.open_stop_orders()
            if type(positions) is not list or type(stops) is not list:
                return block("PROTECTED_GET_ACCOUNT_ROWS_UNVERIFIED")
            orders = []
            deals = {}
            for intent in intents:
                order = await venue.order_by_external_oid(
                    intent.symbol, intent.external_oid,
                )
                if type(order) is not dict:
                    return block("PROTECTED_GET_ORDER_UNKNOWN_NO_RETRY")
                if (
                    order.get("externalOid") != intent.external_oid
                    or order.get("symbol") != intent.symbol
                    or type(order.get("orderId")) not in (str, int)
                    or isinstance(order["orderId"], bool)
                    or not str(order["orderId"]).isdigit()
                    or int(str(order["orderId"])) <= 0
                ):
                    return block("PROTECTED_GET_ORIGIN_ORDER_MISMATCH")
                order_id = str(order["orderId"])
                if order_id in deals:
                    return block("PROTECTED_GET_DUPLICATE_ORDER_ID")
                rows = await venue.order_deal_details(order_id)
                if type(rows) is not list or not rows or any(
                    type(row) is not dict for row in rows
                ):
                    return block("PROTECTED_GET_DEALS_UNKNOWN_NO_RETRY")
                deals[order_id] = rows
                orders.append(order)
            inventory = await completeness.inspect()
            if type(inventory) is not dict:
                return block("PROTECTED_GET_ACCOUNT_COMPLETENESS_UNVERIFIED")
            # Copy snapshots to avoid a mutable client silently changing the
            # first bookend during the second set of reads.
            copies = deepcopy((positions, stops, orders, inventory, deals))
            cp, cs, co, ci, cd = copies
            fingerprint = (
                _venue_digest(cp, cs),
                _hash_rows({
                    "orders": co,
                    "inventory": [ci],
                    "deals": [
                        {"orderId": oid, "records": rows}
                        for oid, rows in sorted(cd.items())
                    ],
                }),
            )
            snapshots.append(copies)
            fingerprints.append(fingerprint)
    except Exception:
        return block("PROTECTED_GET_READ_OR_FINGERPRINT_FAILED_NO_RETRY")

    if fingerprints[0] != fingerprints[1]:
        return block("PROTECTED_GET_BOOKEND_OR_DEALS_DRIFT_NO_RETRY")
    positions, stops, orders, inventory, deals = snapshots[-1]
    try:
        batch = prepare_protected_entries(
            stages=stages,
            intents=intents,
            pre_attempt_witness=pre_attempt_witness,
            orders=orders, positions=positions, stops=stops,
            inventory=inventory, deals_by_order_id=deals,
        )
    except ProtectedFinalizeHalted:
        return block("PROTECTED_GET_FILL_OR_NATIVE_STOP_UNVERIFIED_NO_RETRY")
    if (
        batch.status != "PROTECTED_BATCH_PREPARED_OFFLINE_ONLY"
        or batch.durable_ack_persisted is not False
        or batch.durable_checkpoint_persisted is not False
        or batch.live_authorization is not False
        or batch.mexc_orders_sent is not False
    ):
        return block("PROTECTED_GET_BUNDLE_UNEXPECTED")
    return ProtectedGetPreparation(
        status="PROTECTED_GET_BATCH_PREPARED_NONAUTHORIZING",
        prepared_batch=batch,
        orders_observed=len(orders),
        deal_histories_observed=len(deals),
        two_bookends_equal=True,
    )
