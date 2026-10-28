"""Fail-closed durable V40 attempt journal, deliberately WITHOUT order transport.

A RESERVED slot becomes SUBMITTING only after the matching PREPARED intent
is inserted and both rows transition to SUBMITTING in ONE DB transaction.
There is NO MEXC POST here. Once SUBMITTING is durable, the outcome is
potentially ambiguous after a crash; callers must never blindly retry.
Only a future separately certified exchange coordinator may use this journal.

Every mutation uses the same account advisory TX lock as slot reservation,
an exact active leader fencing token, and unchanged recovery/ledger evidence.
This module does not establish an atomic exchange snapshot, current risk
limits, or permission to trade.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation, ROUND_UP
from typing import Any

from app.t100_live_db_inspect import EXPECTED_DATABASE
from app.t100_live_execution import EntryIntent
from app.t100_live_four_slot_reservations import (
    ACCOUNT_LOCK_SQL, LEASE_SQL, CHECKPOINT_IDS_SQL, RESERVATIONS_SQL,
    UNRESOLVED_INTENTS_SQL, UNRESOLVED_STOPS_SQL, LEGACY_PARENTS_SQL,
    LEGACY_CHILDREN_SQL, SNAPSHOT_AGE_SQL,
)
from app.t100_live_four_slot_snapshot_coordinator import (
    FourSlotSnapshotResult, LedgerSnapshot, _ledger_digest,
)
from app.t100_live_four_slot_ledger_reader import (
    CHECKPOINTS_SQL, REVISIONS_SQL, INTENTS_SQL, AMENDMENTS_SQL,
)
from app.t100_live_leader_fencing import LeaderLease


class FourSlotAttemptHalted(RuntimeError):
    pass


SLOT_ROW_SQL = """
SELECT signal_id, symbol, external_oid, modeled_notional_usdt,
       ledger_sha256, inventory_sha256, snapshot_observed_at,
       holder_id, fencing_token, state
FROM public.t100_live_v40_slot_reservations
WHERE signal_id=$1 FOR UPDATE
"""
PREPARE_INTENT_SQL = """
INSERT INTO public.t100_live_order_intents
(signal_id,symbol,external_oid,reference_price,notional_usdt,
 volume_contracts,stop_price,state)
VALUES ($1,$2,$3,$4,$5,$6,$7,'PREPARED')
ON CONFLICT DO NOTHING RETURNING id
"""
INTENT_SUBMITTING_SQL = """
UPDATE public.t100_live_order_intents
SET state='SUBMITTING', updated_at=clock_timestamp()
WHERE external_oid=$1 AND signal_id=$2 AND state='PREPARED'
RETURNING id
"""
SLOT_SUBMITTING_SQL = """
UPDATE public.t100_live_v40_slot_reservations
SET state='SUBMITTING', updated_at=clock_timestamp()
WHERE signal_id=$1 AND external_oid=$2 AND holder_id=$3
AND fencing_token=$4 AND state='RESERVED'
RETURNING signal_id
"""
INTENT_UNKNOWN_SQL = """
UPDATE public.t100_live_order_intents
SET state='UNKNOWN', failure_reason='EXCHANGE_OUTCOME_UNVERIFIED',
    updated_at=clock_timestamp()
WHERE signal_id=$1 AND external_oid=$2 AND state='SUBMITTING'
RETURNING id
"""
SLOT_UNKNOWN_SQL = """
UPDATE public.t100_live_v40_slot_reservations
SET state='UNKNOWN', updated_at=clock_timestamp()
WHERE signal_id=$1 AND external_oid=$2 AND state='SUBMITTING'
RETURNING signal_id
"""


@dataclass(frozen=True)
class DurableAttemptStage:
    signal_id: int
    external_oid: str
    state: str = "SUBMITTING"
    post_attempted: bool = False
    reconciliation_required: bool = True
    entry_allowed: bool = False
    live_authorization: bool = False


def _d(raw: Any) -> Decimal:
    try:
        v = Decimal(str(raw))
    except (InvalidOperation, TypeError, ValueError):
        raise FourSlotAttemptHalted("INVALID_ENTRY_DECIMAL") from None
    if not v.is_finite() or v <= 0:
        raise FourSlotAttemptHalted("INVALID_ENTRY_DECIMAL")
    return v


def _validate(
    *,
    intent: EntryIntent,
    witness: FourSlotSnapshotResult,
    lease: LeaderLease,
    observed_at: datetime,
    contract_size: Decimal,
    volume_step: Decimal,
    price_tick: Decimal,
) -> None:
    if not isinstance(intent, EntryIntent) or type(intent.signal_id) is not int or intent.signal_id <= 0:
        raise FourSlotAttemptHalted("ENTRY_INTENT_INVALID")
    if (
        not isinstance(witness, FourSlotSnapshotResult)
        or witness.status not in {
            "FLAT_OBSERVED_NONAUTHORIZING",
            "NONFLAT_PROVENANCE_MATCHED_NONAUTHORIZING",
        }
        or witness.entry_allowed is not False
        or witness.live_authorization is not False
        or not isinstance(witness.ledger_hash, str)
        or len(witness.ledger_hash) != 64
        or not isinstance(witness.critical_inventory_hash, str)
        or len(witness.critical_inventory_hash) != 64
    ):
        raise FourSlotAttemptHalted("FOUR_SLOT_WITNESS_INVALID")
    if (
        not isinstance(lease, LeaderLease)
        or lease.lease_name != "t100-live-executor"
        or type(lease.fencing_token) is not int or lease.fencing_token <= 0
        or not lease.holder_id
        or not isinstance(observed_at, datetime)
        or observed_at.tzinfo is None or observed_at.utcoffset() is None
    ):
        raise FourSlotAttemptHalted("FOUR_SLOT_LEASE_OR_CLOCK_INVALID")
    ref, notional, vol, stop = map(
        _d, (intent.reference_price, intent.notional_usdt,
             intent.volume_contracts, intent.stop_price)
    )
    size, step, tick = map(_d, (contract_size, volume_step, price_tick))
    if (
        vol % step != 0
        or notional != ref * size * vol
        or stop != (ref * Decimal("1.75") / tick).to_integral_value(rounding=ROUND_UP) * tick
        or not isinstance(intent.symbol, str)
        or intent.symbol in {"BTC_USDT", "ETH_USDT"}
    ):
        raise FourSlotAttemptHalted("FOUR_SLOT_INTENT_PRICE_LOT_OR_STOP_MISMATCH")


class PostgresFourSlotAttemptJournal:
    """No exchange client. SQL-only; caller must separately certify DB grants."""

    def __init__(self, pool: Any):
        if pool is None:
            raise ValueError("FOUR_SLOT_ATTEMPT_POOL_MISSING")
        self.pool = pool

    async def stage(
        self, *,
        intent: EntryIntent,
        witness: FourSlotSnapshotResult,
        lease: LeaderLease,
        observed_at: datetime,
        contract_size: Decimal,
        volume_step: Decimal,
        price_tick: Decimal,
    ) -> DurableAttemptStage:
        _validate(
            intent=intent, witness=witness, lease=lease, observed_at=observed_at,
            contract_size=contract_size, volume_step=volume_step, price_tick=price_tick,
        )
        try:
            async with self.pool.acquire() as conn:
                async with conn.transaction(isolation="read_committed"):
                    await conn.execute(ACCOUNT_LOCK_SQL)
                    if await conn.fetchval("SELECT current_database()") != EXPECTED_DATABASE:
                        raise FourSlotAttemptHalted("FOUR_SLOT_WRONG_DATABASE")
                    leader = await conn.fetchrow(LEASE_SQL)
                    if (
                        not leader or leader["holder_id"] != lease.holder_id
                        or type(leader["fencing_token"]) is not int
                        or leader["fencing_token"] != lease.fencing_token
                        or leader["unexpired"] is not True
                    ):
                        raise FourSlotAttemptHalted("FOUR_SLOT_LEADER_FENCING_INVALID")
                    row = await conn.fetchrow(SLOT_ROW_SQL, intent.signal_id)
                    if (
                        not row or row["state"] != "RESERVED"
                        or row["symbol"] != intent.symbol
                        or row["external_oid"] != intent.external_oid
                        or row["holder_id"] != lease.holder_id
                        or type(row["fencing_token"]) is not int
                        or row["fencing_token"] != lease.fencing_token
                    ):
                        raise FourSlotAttemptHalted("FOUR_SLOT_RESERVATION_OWNERSHIP_INVALID")
                    if (
                        row["ledger_sha256"] != witness.ledger_hash
                        or row["inventory_sha256"] != witness.critical_inventory_hash
                        or row["snapshot_observed_at"] != observed_at
                    ):
                        raise FourSlotAttemptHalted("FOUR_SLOT_WITNESS_NOT_RESERVED")
                    age = _d(await conn.fetchval(
                        SNAPSHOT_AGE_SQL, observed_at.astimezone(timezone.utc),
                    )) if False else Decimal(str(await conn.fetchval(
                        SNAPSHOT_AGE_SQL, observed_at.astimezone(timezone.utc),
                    )))
                    if not age.is_finite() or not Decimal("-0.25") <= age <= Decimal("2"):
                        raise FourSlotAttemptHalted("FOUR_SLOT_SNAPSHOT_STALE")
                    reserved = _d(row["modeled_notional_usdt"])
                    if intent.notional_usdt > reserved:
                        raise FourSlotAttemptHalted("FOUR_SLOT_INTENT_EXCEEDS_RESERVATION")

                    collections = [
                        [dict(r) for r in await conn.fetch(sql)]
                        for sql in (CHECKPOINTS_SQL, REVISIONS_SQL,
                                    INTENTS_SQL, AMENDMENTS_SQL)
                    ]
                    current = LedgerSnapshot(*collections, readonly_repeatable_read=True)
                    if _ledger_digest(current) != witness.ledger_hash:
                        raise FourSlotAttemptHalted("FOUR_SLOT_LEDGER_DRIFT")

                    active = await conn.fetch(CHECKPOINT_IDS_SQL)
                    cp = {str(r["symbol"]): str(r["position_id"]) for r in active}
                    if len(active) != len(cp) or len(active) != witness.occupied_slots:
                        raise FourSlotAttemptHalted("FOUR_SLOT_CHECKPOINT_DRIFT")
                    if witness.occupied_slots:
                        if witness.recovery is None or cp != {
                            p.symbol: p.position_id for p in witness.recovery.recovered
                        }:
                            raise FourSlotAttemptHalted("FOUR_SLOT_RECOVERY_DRIFT")
                    if intent.symbol in cp:
                        raise FourSlotAttemptHalted("FOUR_SLOT_SYMBOL_ALREADY_OPEN")

                    reservations = await conn.fetch(RESERVATIONS_SQL)
                    symbols = set(cp)
                    same = 0
                    for other in reservations:
                        sym, state = str(other["symbol"]), str(other["state"])
                        if state in {"SUBMITTING", "UNKNOWN", "ACK", "HALTED"}:
                            raise FourSlotAttemptHalted("OTHER_ORDER_OUTCOME_UNRESOLVED")
                        if sym not in cp and sym in symbols:
                            raise FourSlotAttemptHalted("DUPLICATE_RESERVATION_SYMBOL")
                        symbols.add(sym)
                        if int(other["signal_id"]) == intent.signal_id:
                            same += 1
                            if state != "RESERVED" or sym != intent.symbol:
                                raise FourSlotAttemptHalted("RESERVATION_STATE_DRIFT")
                    if same != 1 or len(symbols) > 4:
                        raise FourSlotAttemptHalted("FOUR_SLOT_RESERVATION_COUNT_DRIFT")

                    for sql in (
                        UNRESOLVED_INTENTS_SQL, UNRESOLVED_STOPS_SQL,
                        LEGACY_PARENTS_SQL, LEGACY_CHILDREN_SQL,
                    ):
                        if await conn.fetchval(sql) != 0:
                            raise FourSlotAttemptHalted("OTHER_MUTATION_UNRESOLVED")

                    inserted = await conn.fetchval(
                        PREPARE_INTENT_SQL, intent.signal_id, intent.symbol,
                        intent.external_oid, intent.reference_price,
                        intent.notional_usdt, intent.volume_contracts,
                        intent.stop_price,
                    )
                    if type(inserted) is not int or inserted <= 0:
                        raise FourSlotAttemptHalted("INTENT_ALREADY_PRESENT_OR_NOT_PERSISTED")
                    moved_intent = await conn.fetchval(
                        INTENT_SUBMITTING_SQL, intent.external_oid, intent.signal_id,
                    )
                    moved_slot = await conn.fetchval(
                        SLOT_SUBMITTING_SQL, intent.signal_id, intent.external_oid,
                        lease.holder_id, lease.fencing_token,
                    )
                    if moved_intent != inserted or moved_slot != intent.signal_id:
                        raise FourSlotAttemptHalted("ATOMIC_ATTEMPT_LATCH_FAILED")
                    return DurableAttemptStage(
                        signal_id=intent.signal_id, external_oid=intent.external_oid,
                    )
        except FourSlotAttemptHalted:
            raise
        except Exception:
            raise FourSlotAttemptHalted("FOUR_SLOT_ATTEMPT_DB_FAILED") from None

    async def latch_unknown(
        self, *, signal_id: int, external_oid: str, lease: LeaderLease,
    ) -> None:
        """A confirmed ambiguous submission stays UNKNOWN. No retry endpoint."""
        if type(signal_id) is not int or signal_id <= 0 or not external_oid:
            raise FourSlotAttemptHalted("UNKNOWN_ATTEMPT_IDENTITY_INVALID")
        if (
            not isinstance(lease, LeaderLease)
            or lease.lease_name != "t100-live-executor"
            or type(lease.fencing_token) is not int or lease.fencing_token <= 0
        ):
            raise FourSlotAttemptHalted("UNKNOWN_LEASE_INVALID")
        try:
            async with self.pool.acquire() as conn:
                async with conn.transaction(isolation="read_committed"):
                    await conn.execute(ACCOUNT_LOCK_SQL)
                    if await conn.fetchval("SELECT current_database()") != EXPECTED_DATABASE:
                        raise FourSlotAttemptHalted("FOUR_SLOT_WRONG_DATABASE")
                    leader = await conn.fetchrow(LEASE_SQL)
                    row = await conn.fetchrow(SLOT_ROW_SQL, signal_id)
                    if (
                        not leader or leader["holder_id"] != lease.holder_id
                        or leader["fencing_token"] != lease.fencing_token
                        or leader["unexpired"] is not True
                        or not row or row["state"] != "SUBMITTING"
                        or row["external_oid"] != external_oid
                        or row["holder_id"] != lease.holder_id
                        or row["fencing_token"] != lease.fencing_token
                    ):
                        raise FourSlotAttemptHalted("UNKNOWN_ATTEMPT_NOT_CURRENT_OR_FENCED")
                    intent_id = await conn.fetchval(
                        INTENT_UNKNOWN_SQL, signal_id, external_oid,
                    )
                    slot_id = await conn.fetchval(
                        SLOT_UNKNOWN_SQL, signal_id, external_oid,
                    )
                    if type(intent_id) is not int or slot_id != signal_id:
                        raise FourSlotAttemptHalted("UNKNOWN_ATOMIC_LATCH_FAILED")
        except FourSlotAttemptHalted:
            raise
        except Exception:
            raise FourSlotAttemptHalted("UNKNOWN_ATTEMPT_DB_FAILED") from None
