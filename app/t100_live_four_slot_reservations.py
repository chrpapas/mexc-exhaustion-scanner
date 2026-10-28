"""Offline-testable PostgreSQL atomic slot reservations for frozen T100/V40.

RESERVATION ONLY. Does not authorize orders, create intents, submit MEXC
requests, change stops, run migrations or release reservation records.

Requires separately reviewed migration 031, a future write-restricted role,
and *all* other future writers using the identical account-wide lock.
The database and exchange do not share an atomic snapshot; a reservation is
not proof of a fresh live account. No live connector calls this yet.
"""
from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any

from app.t100_live_db_inspect import EXPECTED_DATABASE
from app.t100_live_four_slot_planner import FourSlotPlan
from app.t100_live_four_slot_snapshot_coordinator import (
    FourSlotSnapshotResult, LedgerSnapshot, _ledger_digest,
)
from app.t100_live_four_slot_ledger_reader import (
    CHECKPOINTS_SQL, REVISIONS_SQL, INTENTS_SQL, AMENDMENTS_SQL,
)
from app.t100_live_leader_fencing import LeaderLease

# Fixed two-key namespace. Lock is transaction-scoped, not process-scoped.
ACCOUNT_LOCK_SQL = "SELECT pg_advisory_xact_lock(10040, 31)"
LEASE_SQL = """
SELECT holder_id, fencing_token,
       (lease_expires_at > clock_timestamp()) AS unexpired
FROM public.t100_live_executor_leases
WHERE lease_name = 't100-live-executor'
FOR UPDATE
"""
CHECKPOINT_IDS_SQL = """
SELECT symbol, position_id
FROM public.t100_live_position_checkpoints
WHERE lifecycle_state <> 'CLOSED'
ORDER BY symbol, position_id
"""
RESERVATIONS_SQL = """
SELECT signal_id, symbol, state
FROM public.t100_live_v40_slot_reservations
WHERE state <> 'RELEASED'
ORDER BY signal_id FOR UPDATE
"""
UNRESOLVED_INTENTS_SQL = """
SELECT count(*) FROM public.t100_live_order_intents
WHERE state IN ('PREPARED','SUBMITTING','UNKNOWN','REVIEW')
"""
UNRESOLVED_STOPS_SQL = """
SELECT count(*) FROM public.t100_live_stop_amendments
WHERE state <> 'VERIFIED'
"""
LEGACY_PARENTS_SQL = """
SELECT count(*) FROM public.t100_live_vol30_parents
WHERE status IN ('ACTIVE','HALTED')
"""
LEGACY_CHILDREN_SQL = """
SELECT count(*) FROM public.t100_live_vol30_children
WHERE state IN ('PREPARED','SUBMITTING','UNKNOWN','ACK','PARTIAL','HALTED')
"""
SNAPSHOT_AGE_SQL = (
    "SELECT EXTRACT(EPOCH FROM (clock_timestamp() - $1::timestamptz))"
)
INSERT_SQL = """
INSERT INTO public.t100_live_v40_slot_reservations
  (signal_id, symbol, external_oid, modeled_notional_usdt,
   ledger_sha256, inventory_sha256, snapshot_observed_at,
   holder_id, fencing_token, state)
VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, 'RESERVED')
RETURNING signal_id
"""

class FourSlotReservationHalted(RuntimeError):
    pass


@dataclass(frozen=True)
class SlotReservation:
    signal_id: int
    symbol: str
    external_oid: str
    modeled_notional_usdt: Decimal
    occupied_after_reservation: int
    journal_state: str = "RESERVED"
    post_attempted: bool = False
    entry_allowed: bool = False
    live_authorization: bool = False


def _sha(value: Any) -> bool:
    return isinstance(value, str) and re.fullmatch(r"[0-9a-f]{64}", value) is not None


def _require_inputs(
    *, plan: FourSlotPlan, witness: FourSlotSnapshotResult,
    observed_at: datetime, lease: LeaderLease, signal_id: int, symbol: str,
) -> Decimal:
    if not isinstance(plan, FourSlotPlan) or not isinstance(witness, FourSlotSnapshotResult):
        raise FourSlotReservationHalted("FOUR_SLOT_MODEL_WITNESS_REQUIRED")
    if (
        plan.action != "ADMIT"
        or plan.entry_allowed is not False or plan.live_authorization is not False
        or plan.v40_admission is None
        or plan.v40_admission.action != "ADMIT"
        or plan.occupied_slots != witness.occupied_slots
        or type(witness.occupied_slots) is not int
        or not 0 <= witness.occupied_slots < 4
        or witness.entry_allowed is not False
        or witness.live_authorization is not False
        or witness.status not in {
            "FLAT_OBSERVED_NONAUTHORIZING",
            "NONFLAT_PROVENANCE_MATCHED_NONAUTHORIZING",
        }
        or not isinstance(witness.inventory, dict)
        or witness.inventory.get("account_wide_completeness_verified") is not True
        or witness.inventory.get("critical_inventory_equal_across_witness_window") is not True
        or not _sha(witness.ledger_hash) or not _sha(witness.critical_inventory_hash)
        or type(signal_id) is not int or signal_id <= 0
        or not isinstance(symbol, str)
        or re.fullmatch(r"[A-Z0-9]+_USDT", symbol) is None
        or symbol in {"BTC_USDT", "ETH_USDT"}
    ):
        raise FourSlotReservationHalted("FOUR_SLOT_ADMISSION_EVIDENCE_BLOCKED")
    if witness.occupied_slots and (
        witness.recovery is None
        or len(witness.recovery.recovered) != witness.occupied_slots
        or symbol in {p.symbol for p in witness.recovery.recovered}
    ):
        raise FourSlotReservationHalted("FOUR_SLOT_SYMBOL_OR_RECOVERY_BLOCKED")
    if not witness.occupied_slots and witness.recovery is not None:
        raise FourSlotReservationHalted("FOUR_SLOT_FLAT_RECOVERY_MISMATCH")
    if (
        not isinstance(lease, LeaderLease)
        or lease.lease_name != "t100-live-executor"
        or not isinstance(lease.holder_id, str) or not lease.holder_id
        or type(lease.fencing_token) is not int or lease.fencing_token <= 0
        or type(observed_at) is not datetime
        or observed_at.tzinfo is None or observed_at.utcoffset() is None
    ):
        raise FourSlotReservationHalted("FOUR_SLOT_LEASE_OR_TIME_INVALID")
    try:
        modeled = Decimal(str(plan.modeled_notional_usdt))
        frozen = Decimal(str(plan.v40_admission.modeled_notional_usdt))
    except Exception as exc:
        raise FourSlotReservationHalted("FOUR_SLOT_NOTIONAL_INVALID") from None
    if not modeled.is_finite() or modeled <= 0 or modeled != frozen:
        raise FourSlotReservationHalted("FOUR_SLOT_NOTIONAL_MISMATCH")
    return modeled


class PostgresFourSlotReservations:
    """SERIALIZE cooperating writers under a global account advisory TX lock.

    This alone cannot guarantee exchange execution safety. A subsequent
    coordinator must revalidate all live inputs before any MEXC POST, and
    permit issuance remains prohibited while the adapter is uncertified.
    """

    def __init__(self, pool: Any):
        if pool is None:
            raise ValueError("FOUR_SLOT_POOL_MISSING")
        self.pool = pool

    async def reserve(
        self, *,
        plan: FourSlotPlan, witness: FourSlotSnapshotResult,
        observed_at: datetime, lease: LeaderLease,
        signal_id: int, symbol: str,
    ) -> SlotReservation:
        modeled = _require_inputs(
            plan=plan, witness=witness, observed_at=observed_at,
            lease=lease, signal_id=signal_id, symbol=symbol,
        )
        oid = "t100v40r" + hashlib.sha256(
            f"{signal_id}:{symbol}:FROZEN_V40".encode("utf-8")
        ).hexdigest()[:20]
        try:
            async with self.pool.acquire() as conn:
                # READ COMMITTED: next SELECT sees changes committed while
                # waiting on the serialization lock. The lock is acquired
                # BEFORE any data-dependent read or the reservation INSERT.
                async with conn.transaction(isolation="read_committed"):
                    await conn.execute(ACCOUNT_LOCK_SQL)
                    if await conn.fetchval("SELECT current_database()") != EXPECTED_DATABASE:
                        raise FourSlotReservationHalted("FOUR_SLOT_WRONG_DATABASE")
                    leader = await conn.fetchrow(LEASE_SQL)
                    if (
                        leader is None or leader["holder_id"] != lease.holder_id
                        or type(leader["fencing_token"]) is not int
                        or leader["fencing_token"] != lease.fencing_token
                        or leader["unexpired"] is not True
                    ):
                        raise FourSlotReservationHalted("FOUR_SLOT_LEADER_FENCING_INVALID")

                    age = Decimal(str(await conn.fetchval(
                        SNAPSHOT_AGE_SQL, observed_at.astimezone(timezone.utc)
                    )))
                    if not age.is_finite() or not Decimal("-0.25") <= age <= Decimal("2.0"):
                        raise FourSlotReservationHalted("FOUR_SLOT_SNAPSHOT_TOO_OLD")

                    parts = []
                    for query in (
                        CHECKPOINTS_SQL, REVISIONS_SQL,
                        INTENTS_SQL, AMENDMENTS_SQL,
                    ):
                        parts.append([dict(row) for row in await conn.fetch(query)])
                    current = LedgerSnapshot(*parts, readonly_repeatable_read=True)
                    if _ledger_digest(current) != witness.ledger_hash:
                        raise FourSlotReservationHalted("FOUR_SLOT_LEDGER_CHANGED")

                    checkpoint_ids = await conn.fetch(CHECKPOINT_IDS_SQL)
                    cp = {str(r["symbol"]): str(r["position_id"]) for r in checkpoint_ids}
                    if len(cp) != len(checkpoint_ids) or len(cp) != witness.occupied_slots:
                        raise FourSlotReservationHalted("FOUR_SLOT_CHECKPOINT_SET_CHANGED")
                    if witness.occupied_slots:
                        recovered = {r.symbol: r.position_id for r in witness.recovery.recovered}
                        if cp != recovered:
                            raise FourSlotReservationHalted("FOUR_SLOT_POSITION_OWNERSHIP_DRIFT")

                    reservations = await conn.fetch(RESERVATIONS_SQL)
                    reserved_symbols = set()
                    for row in reservations:
                        rsym = str(row["symbol"])
                        state = str(row["state"])
                        if (
                            state in {"SUBMITTING", "UNKNOWN", "ACK", "HALTED"}
                            or rsym in reserved_symbols
                            or (rsym in cp and state != "PROTECTED")
                            or (state == "PROTECTED" and rsym not in cp)
                        ):
                            raise FourSlotReservationHalted("FOUR_SLOT_UNRESOLVED_RESERVATION")
                        reserved_symbols.add(rsym)
                    occupied = set(cp) | reserved_symbols
                    if (
                        len(occupied) >= 4
                        or symbol in occupied
                        or any(int(row["signal_id"]) == signal_id for row in reservations)
                    ):
                        raise FourSlotReservationHalted("FOUR_SLOT_CAP_OR_DUPLICATE")
                    for query in (
                        UNRESOLVED_INTENTS_SQL, UNRESOLVED_STOPS_SQL,
                        LEGACY_PARENTS_SQL, LEGACY_CHILDREN_SQL,
                    ):
                        count = await conn.fetchval(query)
                        if type(count) is not int or count != 0:
                            raise FourSlotReservationHalted("FOUR_SLOT_OTHER_MUTATION_UNRESOLVED")

                    inserted = await conn.fetchval(
                        INSERT_SQL, signal_id, symbol, oid, modeled,
                        witness.ledger_hash, witness.critical_inventory_hash,
                        observed_at.astimezone(timezone.utc),
                        lease.holder_id, lease.fencing_token,
                    )
                    if type(inserted) is not int or inserted != signal_id:
                        raise FourSlotReservationHalted("FOUR_SLOT_RESERVATION_NOT_PERSISTED")
                    return SlotReservation(
                        signal_id=signal_id, symbol=symbol, external_oid=oid,
                        modeled_notional_usdt=modeled,
                        occupied_after_reservation=len(occupied)+1,
                    )
        except FourSlotReservationHalted:
            raise
        except Exception:
            # Never expose connection credentials, SQL parameters or raw IDs.
            raise FourSlotReservationHalted("FOUR_SLOT_RESERVATION_DB_FAILED") from None
