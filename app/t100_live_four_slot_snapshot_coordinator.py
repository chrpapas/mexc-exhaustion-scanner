"""GET/SELECT-only, nonauthorizing 0–4 slot provenance reconciliation.

The ledger reader contract requires *one read-only REPEATABLE READ transaction*
per read_snapshot() call; this module does not implement or certify that DB
adapter. Exchange reads are non-atomic; bookending detects observed drift but
cannot establish atomicity, prevent ABA changes, or grant POST authorization.
No SQL writes, MEXC mutations, or live permits occur here.
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from typing import Any, Protocol

from app.t100_live_consistency import (
    CRITICAL_POSITION_FIELDS, CRITICAL_STOP_FIELDS,
)
from app.t100_live_four_slot_stop_count_diagnostic import (
    classify_current_position_stop_mismatch,
)
from app.t100_live_durable_recovery import (
    DurableRecoveryHalted, DurableRecoveryReport,
    checkpoint_from_row, recover_nonflat,
)
from app.t100_live_shared_account_partition import (
    SharedPartitionHalted, partition_shared_account,
)

MAX_SLOTS = 4


@dataclass(frozen=True)
class LedgerSnapshot:
    checkpoints: list[dict[str, Any]]
    revisions: list[dict[str, Any]]
    intents: list[dict[str, Any]]
    stop_amendments: list[dict[str, Any]]
    # This is a reader assertion, NOT independent DB-role certification.
    readonly_repeatable_read: bool


@dataclass(frozen=True)
class FourSlotSnapshotResult:
    status: str
    occupied_slots: int = 0
    recovery: DurableRecoveryReport | None = None
    inventory: dict[str, Any] | None = None
    ledger_hash: str | None = None
    critical_inventory_hash: str | None = None
    mismatch_diagnostic: dict[str, Any] | None = None
    manual_positions_observed: int | None = None
    manual_positions_without_positive_sl_observed: int | None = None
    account_positions_observed: int | None = None
    entry_allowed: bool = False
    live_authorization: bool = False


class LedgerSnapshotReader(Protocol):
    async def read_snapshot(self) -> LedgerSnapshot: ...


class VenueReader(Protocol):
    async def open_positions(self) -> list[dict[str, Any]]: ...
    async def open_stop_orders(self) -> list[dict[str, Any]]: ...
    async def order_by_external_oid(
        self, symbol: str, external_oid: str
    ) -> dict[str, Any] | None: ...


class InventoryCompletenessReader(Protocol):
    async def inspect(self) -> dict[str, Any]: ...


def _encode(value: Any) -> Any:
    if isinstance(value, Decimal):
        if not value.is_finite():
            raise ValueError("NONFINITE_DECIMAL")
        return str(value)
    if isinstance(value, datetime):
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("NAIVE_DATETIME")
        return value.isoformat()
    raise TypeError("UNSUPPORTED_SNAPSHOT_FIELD")


def _hash_rows(collections: dict[str, Any]) -> str:
    """Hash ordered *sets* of rows; no user data is printed or logged."""
    data = {}
    for key, rows in collections.items():
        if not isinstance(rows, list) or any(type(r) is not dict for r in rows):
            raise ValueError("INVALID_SNAPSHOT_ROWS")
        rendered = [
            json.dumps(row, default=_encode, sort_keys=True,
                       allow_nan=False, separators=(",", ":"))
            for row in rows
        ]
        data[key] = sorted(rendered)
    raw = json.dumps(data, sort_keys=True, allow_nan=False, separators=(",", ":"))
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _ledger_digest(snap: LedgerSnapshot) -> str:
    if not isinstance(snap, LedgerSnapshot) or snap.readonly_repeatable_read is not True:
        raise ValueError("LEDGER_SNAPSHOT_CONTRACT_UNVERIFIED")
    return _hash_rows({
        "checkpoints": snap.checkpoints,
        "revisions": snap.revisions,
        "intents": snap.intents,
        "amendments": snap.stop_amendments,
    })


def _venue_digest(
    positions: list[dict[str, Any]], stops: list[dict[str, Any]],
) -> str:
    return _hash_rows({
        "positions": [
            {key: row[key] for key in CRITICAL_POSITION_FIELDS if key in row}
            for row in positions
        ],
        "stops": [
            {key: row[key] for key in CRITICAL_STOP_FIELDS if key in row}
            for row in stops
        ],
    })


def _valid_counts(report: Any, npositions: int, nstops: int) -> bool:
    if not isinstance(report, dict):
        return False
    if (
        report.get("account_wide_completeness_verified") is not True
        or report.get("critical_inventory_equal_across_witness_window") is not True
        or any(type(report.get(k)) is not int for k in (
            "before_positions", "after_positions",
            "before_current_tpsl", "after_current_tpsl",
        ))
        or report["before_positions"] != npositions
        or report["after_positions"] != npositions
        or report["before_current_tpsl"] != nstops
        or report["after_current_tpsl"] != nstops
    ):
        return False
    counts = report.get("open_order_counts")
    keys = ("sumCount", "limitOrderCount", "stopOrderCount",
            "planOrderCount", "trackOrderCount")
    if (
        not isinstance(counts, dict)
        or any(type(counts.get(k)) is not int or counts[k] < 0 for k in keys)
        or counts["stopOrderCount"] != nstops
        or counts["sumCount"] != sum(counts[k] for k in keys[1:])
        or any(counts[k] for k in ("limitOrderCount", "planOrderCount", "trackOrderCount"))
    ):
        return False
    return True


class FourSlotSnapshotCoordinator:
    def __init__(
        self, *,
        ledger: LedgerSnapshotReader,
        venue: VenueReader,
        completeness: InventoryCompletenessReader,
    ) -> None:
        self.ledger = ledger
        self.venue = venue
        self.completeness = completeness

    async def inspect(
        self, *, shared_manual_observation: bool = False,
    ) -> FourSlotSnapshotResult:
        """Observe without writing. Shared mode is NEVER order authorization."""
        if type(shared_manual_observation) is not bool:
            return FourSlotSnapshotResult("SHARED_MODE_INPUT_INVALID")
        try:
            first = await self.ledger.read_snapshot()
            first_hash = _ledger_digest(first)
        except Exception:
            return FourSlotSnapshotResult("LEDGER_READ_OR_CONTRACT_FAILED")

        try:
            before_positions = await self.venue.open_positions()
            before_stops = await self.venue.open_stop_orders()
            before_hash = _venue_digest(before_positions, before_stops)
            if len(before_positions) > MAX_SLOTS:
                return FourSlotSnapshotResult("ACCOUNT_EXCEEDS_FOUR_SLOTS")
            report = await self.completeness.inspect()
        except Exception:
            return FourSlotSnapshotResult("INVENTORY_READ_OR_FORMAT_FAILED")

        # Fetch the exact order origins from durable checkpoint identities.
        # A non-flat account without a checkpoint cannot be adopted.
        try:
            checkpoints = [checkpoint_from_row(r) for r in first.checkpoints]
            orders = []
            for cp in checkpoints:
                found = await self.venue.order_by_external_oid(
                    cp.symbol, cp.external_oid,
                )
                if not isinstance(found, dict):
                    return FourSlotSnapshotResult("ORIGIN_ORDER_UNAVAILABLE")
                orders.append(found)
            second = await self.ledger.read_snapshot()
            second_hash = _ledger_digest(second)
            after_positions = await self.venue.open_positions()
            after_stops = await self.venue.open_stop_orders()
            after_hash = _venue_digest(after_positions, after_stops)
        except DurableRecoveryHalted:
            return FourSlotSnapshotResult("CHECKPOINT_IDENTITY_INVALID")
        except Exception:
            return FourSlotSnapshotResult("RECOVERY_BOOKEND_READ_FAILED")

        if first_hash != second_hash:
            return FourSlotSnapshotResult("LEDGER_CHANGED_DURING_OBSERVATION")
        if before_hash != after_hash:
            return FourSlotSnapshotResult("VENUE_CRITICAL_INVENTORY_CHANGED")
        if not _valid_counts(report, len(after_positions), len(after_stops)):
            return FourSlotSnapshotResult("INVENTORY_COMPLETENESS_UNVERIFIED")
        if shared_manual_observation:
            # Full-account counts, critical hashes and read-only ledger
            # equality are already proven above. Partition by durable
            # checkpoint ID, NEVER by caller-supplied "manual" labels.
            # Historical/restart and post-submission gates must separately
            # accept this topology before any live order is even considered.
            try:
                partition = partition_shared_account(
                    checkpoints=checkpoints,
                    positions=after_positions, stops=after_stops,
                )
            except SharedPartitionHalted:
                return FourSlotSnapshotResult(
                    "SHARED_ACCOUNT_POSITION_PARTITION_UNVERIFIED",
                )
            if not checkpoints:
                if first.revisions or first.intents or first.stop_amendments:
                    return FourSlotSnapshotResult(
                        "SHARED_ACCOUNT_T100_LEDGER_HISTORY_UNVERIFIED",
                    )
                return FourSlotSnapshotResult(
                    status="SHARED_ACCOUNT_MANUAL_ONLY_NONAUTHORIZING",
                    manual_positions_observed=partition.manual_positions,
                    manual_positions_without_positive_sl_observed=(
                        partition.manual_positions_without_positive_native_sl
                    ),
                    account_positions_observed=partition.account_positions,
                    inventory=report,
                    ledger_hash=first_hash,
                    critical_inventory_hash=after_hash,
                )
            try:
                recovery = recover_nonflat(
                    checkpoint_rows=first.checkpoints,
                    revision_rows=first.revisions,
                    intent_rows=first.intents,
                    stop_amendment_rows=first.stop_amendments,
                    orders=orders,
                    positions=partition.managed_positions,
                    stops=partition.managed_stops,
                    positions_complete=True, stops_complete=True,
                )
            except (DurableRecoveryHalted, TypeError, ValueError):
                return FourSlotSnapshotResult(
                    "SHARED_ACCOUNT_T100_DURABLE_PROVENANCE_UNVERIFIED",
                )
            return FourSlotSnapshotResult(
                status="SHARED_ACCOUNT_MANAGED_PROVENANCE_MATCHED_NONAUTHORIZING",
                occupied_slots=len(checkpoints),
                manual_positions_observed=partition.manual_positions,
                manual_positions_without_positive_sl_observed=(
                    partition.manual_positions_without_positive_native_sl
                ),
                account_positions_observed=partition.account_positions,
                recovery=recovery,
                inventory=report,
                ledger_hash=first_hash,
                critical_inventory_hash=after_hash,
            )
        if len(after_positions) != len(after_stops):
            # Never reinterpret multiple TP/SL records as adequate protection.
            # Provide only aggregate topology from the SAME already verified
            # stable GET window. Do not print account IDs or trade amounts.
            return FourSlotSnapshotResult(
                "POSITION_STOP_COUNT_MISMATCH",
                mismatch_diagnostic=classify_current_position_stop_mismatch(
                    after_positions, after_stops,
                ),
            )

        n = len(after_positions)
        if n != len(checkpoints):
            return FourSlotSnapshotResult("UNOWNED_OR_MISSING_CHECKPOINTS")
        if n == 0:
            # Historical ACKs can exist after completed trades. This
            # coordinator has not yet certified the CLOSED lifecycle,
            # so it must not declare a dirty historical ledger safe/flat.
            if first.revisions or first.intents or first.stop_amendments:
                return FourSlotSnapshotResult("FLAT_HISTORY_REQUIRES_CLOSED_LEDGER_AUDIT")
            return FourSlotSnapshotResult(
                status="FLAT_OBSERVED_NONAUTHORIZING",
                inventory=report, ledger_hash=first_hash,
                critical_inventory_hash=after_hash,
            )
        try:
            recovery = recover_nonflat(
                checkpoint_rows=first.checkpoints,
                revision_rows=first.revisions,
                intent_rows=first.intents,
                stop_amendment_rows=first.stop_amendments,
                orders=orders,
                positions=after_positions,
                stops=after_stops,
                positions_complete=True,
                stops_complete=True,
            )
        except (DurableRecoveryHalted, TypeError, ValueError):
            return FourSlotSnapshotResult("DURABLE_EXCHANGE_PROVENANCE_MISMATCH")
        return FourSlotSnapshotResult(
            status="NONFLAT_PROVENANCE_MATCHED_NONAUTHORIZING",
            occupied_slots=n,
            recovery=recovery,
            inventory=report,
            ledger_hash=first_hash,
            critical_inventory_hash=after_hash,
        )
