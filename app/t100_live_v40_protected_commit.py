"""Unwired V40 protected-fill persistence: ONE DB transaction, NO MEXC POST.

Consumes a prevalidated ProtectedBatch plus leader/witness evidence, then
moves matching SUBMITTING order intents and slot rows to ACK/PROTECTED,
inserting immutable fill sets, fills, and version-1 OPEN checkpoints/revisions
atomically under the common account advisory transaction lock.

THIS IS AN OFFLINE-TESTED, UNDEPLOYED DB ADAPTER. It cannot independently
certify current MEXC inventory/atomicity, its supplied preflight evidence,
fencing across systems or live order readiness. Migration 031 is unapplied.
No scheduler or live runtime calls this adapter. Do not add one without an
independent production review and complete cross-margin risk/stop gates.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from typing import Any

from app.t100_live_checkpoint_journal import (
    ENTRY_PROTECTED, CheckpointJournalHalted,
    PostgresCheckpointJournal, validate_checkpoint_transition,
)
from app.t100_live_db_inspect import EXPECTED_DATABASE
from app.t100_live_four_slot_attempt_journal import SLOT_ROW_SQL
from app.t100_live_four_slot_reservations import (
    ACCOUNT_LOCK_SQL, LEASE_SQL, CHECKPOINT_IDS_SQL,
    RESERVATIONS_SQL, SNAPSHOT_AGE_SQL, UNRESOLVED_STOPS_SQL,
    LEGACY_PARENTS_SQL, LEGACY_CHILDREN_SQL,
)
from app.t100_live_four_slot_snapshot_coordinator import FourSlotSnapshotResult
from app.t100_live_leader_fencing import LeaderLease
from app.t100_live_v40_protected_finalization import PreparedProtectedBatch


class ProtectedCommitHalted(RuntimeError):
    pass


UNRESOLVED_IDENTITIES_SQL = """
SELECT external_oid, state FROM public.t100_live_order_intents
WHERE state IN ('PREPARED','SUBMITTING','UNKNOWN','REVIEW')
ORDER BY external_oid FOR UPDATE
"""

CURRENT_INTENT_SQL = """
SELECT id,signal_id,symbol,external_oid,exchange_order_id,
       reference_price,notional_usdt,volume_contracts,stop_price,state
FROM public.t100_live_order_intents
WHERE external_oid=$1 FOR UPDATE
"""

ACK_INTENT_SQL = """
UPDATE public.t100_live_order_intents
SET state='ACK',exchange_order_id=$3,updated_at=clock_timestamp()
WHERE external_oid=$1 AND signal_id=$2 AND state='SUBMITTING'
  AND exchange_order_id IS NULL
RETURNING id
"""

INSERT_FILL_SET_SQL = """
INSERT INTO public.t100_live_order_fill_sets
(order_id,position_id,symbol,side,order_state,deal_volume,
 average_price,fee_currency,fill_count,fill_digest_sha256,
 strategy_fully_filled_order_complete)
VALUES($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11)
ON CONFLICT DO NOTHING RETURNING order_id
"""

INSERT_FILL_SQL = """
INSERT INTO public.t100_live_order_fills
(trade_id,order_id,position_id,side,contracts,price,fee_usdt,timestamp_ms,category)
VALUES($1,$2,$3,$4,$5,$6,$7,$8,$9)
ON CONFLICT DO NOTHING RETURNING trade_id
"""

INSERT_CHECKPOINT_SQL = """
INSERT INTO public.t100_live_position_checkpoints
(position_id,symbol,external_oid,exchange_order_id,contracts,
 entry_price,stop_order_id,stop_price,best_profit_pct,armed,
 lifecycle_state,checkpoint_version,last_write_reason)
VALUES($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12,$13)
ON CONFLICT DO NOTHING RETURNING position_id
"""

INSERT_REVISION_SQL = """
INSERT INTO public.t100_live_position_checkpoint_revisions
(position_id,checkpoint_version,write_reason,symbol,external_oid,
 exchange_order_id,contracts,entry_price,stop_order_id,stop_price,
 best_profit_pct,armed,lifecycle_state)
VALUES($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12,$13)
ON CONFLICT DO NOTHING RETURNING revision_id
"""

SLOT_ACK_SQL = """
UPDATE public.t100_live_v40_slot_reservations
SET state='ACK',updated_at=clock_timestamp()
WHERE signal_id=$1 AND external_oid=$2 AND state='SUBMITTING'
  AND holder_id=$3 AND fencing_token=$4
RETURNING signal_id
"""

SLOT_PROTECTED_SQL = """
UPDATE public.t100_live_v40_slot_reservations
SET state='PROTECTED',updated_at=clock_timestamp()
WHERE signal_id=$1 AND external_oid=$2 AND state='ACK'
  AND holder_id=$3 AND fencing_token=$4
RETURNING signal_id
"""


@dataclass(frozen=True)
class ProtectedCommitResult:
    status: str
    entries_committed: int
    db_atomic_commit_observed: bool = False
    venue_state_currently_verified: bool = False
    exchange_atomicity_verified: bool = False
    real_post_attempted: bool = False
    entry_allowed: bool = False
    live_authorization: bool = False
    mexc_orders_sent: bool = False


def _number(raw: Any) -> Decimal:
    if raw is None or type(raw) is bool:
        raise ProtectedCommitHalted("INVALID_PROTECTED_DECIMAL")
    try:
        n = Decimal(str(raw))
    except (InvalidOperation, ValueError, TypeError):
        raise ProtectedCommitHalted("INVALID_PROTECTED_DECIMAL") from None
    if not n.is_finite() or n <= 0:
        raise ProtectedCommitHalted("INVALID_PROTECTED_DECIMAL")
    return n


def _require(
    batch: PreparedProtectedBatch,
    witness: FourSlotSnapshotResult,
    leader: LeaderLease,
    observed_at: datetime,
) -> None:
    if (
        type(batch) is not PreparedProtectedBatch
        or batch.status != "PROTECTED_BATCH_PREPARED_OFFLINE_ONLY"
        or not 1 <= len(batch.entries) <= 4
        or batch.durable_ack_persisted is not False
        or batch.durable_checkpoint_persisted is not False
        or batch.live_authorization is not False
        or batch.mexc_orders_sent is not False
        or type(witness) is not FourSlotSnapshotResult
        or witness.status not in {
            "FLAT_OBSERVED_NONAUTHORIZING",
            "NONFLAT_PROVENANCE_MATCHED_NONAUTHORIZING",
        }
        or type(witness.occupied_slots) is not int
        or not 0 <= witness.occupied_slots <= 3
        or witness.occupied_slots + len(batch.entries) > 4
        or not isinstance(witness.ledger_hash, str)
        or len(witness.ledger_hash) != 64
        or witness.entry_allowed is not False
        or witness.live_authorization is not False
        or not isinstance(leader, LeaderLease)
        or leader.lease_name != "t100-live-executor"
        or not leader.holder_id
        or type(leader.fencing_token) is not int
        or leader.fencing_token <= 0
        or type(observed_at) is not datetime
        or observed_at.tzinfo is None
        or observed_at.utcoffset() is None
    ):
        raise ProtectedCommitHalted("PREPARED_BATCH_WITNESS_OR_LEASE_INVALID")
    expected = set()
    pos_ids = set()
    order_ids = set()
    trade_ids = set()
    for item in batch.entries:
        intent, stage, cp, fills = (
            item.intent, item.stage, item.checkpoint, item.fill_set,
        )
        try:
            validate_checkpoint_transition(None, cp, reason=ENTRY_PROTECTED)
        except CheckpointJournalHalted:
            raise ProtectedCommitHalted("CHECKPOINT_TRANSITION_INVALID") from None
        if (
            stage.state != "SUBMITTING" or stage.post_attempted is not False
            or stage.reconciliation_required is not True
            or stage.entry_allowed is not False
            or stage.live_authorization is not False
            or stage.signal_id != intent.signal_id
            or stage.external_oid != intent.external_oid
            or cp.external_oid != intent.external_oid
            or cp.symbol != intent.symbol
            or cp.contracts != intent.volume_contracts
            or cp.exchange_order_id != item.order_id
            or cp.position_id != item.position_id
            or fills.order_id != item.order_id
            or fills.position_id != item.position_id
            or fills.symbol != intent.symbol
            or fills.side != "OPEN_SHORT"
            or fills.fee_currency != "USDT"
            or fills.order_state != 3
            or fills.deal_volume != cp.contracts
            or fills.average_price != cp.entry_price
            or fills.strategy_fully_filled_order_complete is not True
            or fills.final_order_fill_history_complete is not True
            or not fills.fills
        ):
            raise ProtectedCommitHalted("PREPARED_ENTRY_IDENTITY_MISMATCH")
        key = (intent.signal_id, intent.external_oid, intent.symbol)
        if (key in expected or item.position_id in pos_ids
            or item.order_id in order_ids):
            raise ProtectedCommitHalted("DUPLICATE_PREPARED_ENTRY")
        expected.add(key)
        pos_ids.add(item.position_id)
        order_ids.add(item.order_id)
        for fill in fills.fills:
            if fill.trade_id in trade_ids or fill.order_id != item.order_id:
                raise ProtectedCommitHalted("DUPLICATE_OR_DRIFTED_FILL_ID")
            trade_ids.add(fill.trade_id)


class PostgresProtectedV40Commit:
    """Atomic DB record finalization ONLY. Never performs a venue request."""

    def __init__(self, pool: Any):
        if pool is None:
            raise ValueError("PROTECTED_COMMIT_POOL_MISSING")
        self.pool = pool

    async def persist(
        self, *,
        batch: PreparedProtectedBatch,
        pre_attempt_witness: FourSlotSnapshotResult,
        lease: LeaderLease,
        observed_at: datetime,
    ) -> ProtectedCommitResult:
        _require(batch, pre_attempt_witness, lease, observed_at)
        expected_oids = {x.intent.external_oid for x in batch.entries}
        expected_old = (
            {} if pre_attempt_witness.recovery is None else {
                p.symbol: p.position_id
                for p in pre_attempt_witness.recovery.recovered
            }
        )
        if len(expected_old) != pre_attempt_witness.occupied_slots:
            raise ProtectedCommitHalted("PREEXISTING_OWNERSHIP_NOT_RECONCILED")
        if expected_old.keys() & {x.intent.symbol for x in batch.entries}:
            raise ProtectedCommitHalted("OWNED_SYMBOL_ALREADY_PRESENT")

        try:
            async with self.pool.acquire() as conn:
                async with conn.transaction(isolation="read_committed"):
                    # Same 2-key account-wide TX lock as prior journal writers.
                    await conn.execute(ACCOUNT_LOCK_SQL)
                    if await conn.fetchval("SELECT current_database()") != EXPECTED_DATABASE:
                        raise ProtectedCommitHalted("PROTECTED_COMMIT_WRONG_DATABASE")
                    current_leader = await conn.fetchrow(LEASE_SQL)
                    if (
                        not current_leader
                        or current_leader["holder_id"] != lease.holder_id
                        or current_leader["fencing_token"] != lease.fencing_token
                        or current_leader["unexpired"] is not True
                    ):
                        raise ProtectedCommitHalted("PROTECTED_COMMIT_LEASE_INVALID")
                    try:
                        age = Decimal(str(await conn.fetchval(
                            SNAPSHOT_AGE_SQL, observed_at.astimezone(timezone.utc),
                        )))
                    except (InvalidOperation, ValueError, TypeError):
                        raise ProtectedCommitHalted("PROTECTED_COMMIT_SNAPSHOT_TIME_INVALID") from None
                    if not age.is_finite() or not Decimal("-0.25") <= age <= Decimal("2"):
                        raise ProtectedCommitHalted("PROTECTED_COMMIT_SNAPSHOT_STALE")

                    active = await conn.fetch(CHECKPOINT_IDS_SQL)
                    previous = {str(x["symbol"]): str(x["position_id"]) for x in active}
                    if len(previous) != len(active) or previous != expected_old:
                        raise ProtectedCommitHalted("PREEXISTING_CHECKPOINT_SET_DRIFT")
                    slots = await conn.fetch(RESERVATIONS_SQL)
                    known = {(x.intent.signal_id, x.intent.external_oid, x.intent.symbol)
                             for x in batch.entries}
                    staged = set()
                    for row in slots:
                        state = row["state"]
                        key = (row["signal_id"], row.get("external_oid"), row["symbol"])
                        # The shared RESERVATIONS_SQL projection may only include
                        # signal/symbol/state. Exact OIDs are re-read below.
                        if state == "SUBMITTING" and row["symbol"] in {
                            x.intent.symbol for x in batch.entries
                        }:
                            staged.add((row["signal_id"], row["symbol"]))
                        elif state == "PROTECTED" and row["symbol"] in expected_old:
                            pass
                        else:
                            raise ProtectedCommitHalted("OTHER_SLOT_UNRESOLVED")
                    if staged != {(x.intent.signal_id, x.intent.symbol)
                                  for x in batch.entries}:
                        raise ProtectedCommitHalted("STAGED_SLOT_SET_DRIFT")
                    unresolved = await conn.fetch(UNRESOLVED_IDENTITIES_SQL)
                    if (
                        len(unresolved) != len(expected_oids)
                        or {x["external_oid"] for x in unresolved} != expected_oids
                        or any(x["state"] != "SUBMITTING" for x in unresolved)
                    ):
                        raise ProtectedCommitHalted("OTHER_INTENT_UNRESOLVED")
                    for query in (UNRESOLVED_STOPS_SQL,
                                  LEGACY_PARENTS_SQL, LEGACY_CHILDREN_SQL):
                        if await conn.fetchval(query) != 0:
                            raise ProtectedCommitHalted("OTHER_MUTATION_UNRESOLVED")
                    for entry in batch.entries:
                        intent, stage = entry.intent, entry.stage
                        reservation = await conn.fetchrow(
                            SLOT_ROW_SQL, intent.signal_id,
                        )
                        current = await conn.fetchrow(
                            CURRENT_INTENT_SQL, intent.external_oid,
                        )
                        if (
                            not reservation or not current
                            or reservation["state"] != "SUBMITTING"
                            or reservation["symbol"] != intent.symbol
                            or reservation["external_oid"] != intent.external_oid
                            or reservation["holder_id"] != lease.holder_id
                            or reservation["fencing_token"] != lease.fencing_token
                            or current["state"] != "SUBMITTING"
                            or current.get("exchange_order_id") is not None
                            or current["signal_id"] != intent.signal_id
                            or current["symbol"] != intent.symbol
                            or current["external_oid"] != intent.external_oid
                            or _number(current["reference_price"]) != intent.reference_price
                            or _number(current["notional_usdt"]) != intent.notional_usdt
                            or _number(current["volume_contracts"]) != intent.volume_contracts
                            or _number(current["stop_price"]) != intent.stop_price
                        ):
                            raise ProtectedCommitHalted("STAGED_INTENT_OR_SLOT_DRIFT")
                    for entry in batch.entries:
                        intent, cp, fills = (
                            entry.intent, entry.checkpoint, entry.fill_set,
                        )
                        ack = await conn.fetchval(
                            ACK_INTENT_SQL, intent.external_oid,
                            intent.signal_id, entry.order_id,
                        )
                        if type(ack) is not int or ack <= 0:
                            raise ProtectedCommitHalted("ACK_INTENT_CAS_FAILED")
                        persisted = await conn.fetchval(
                            INSERT_FILL_SET_SQL,
                            fills.order_id, fills.position_id, fills.symbol,
                            fills.side, fills.order_state, fills.deal_volume,
                            fills.average_price, fills.fee_currency,
                            len(fills.fills), fills.fill_digest_sha256, True,
                        )
                        if persisted != fills.order_id:
                            raise ProtectedCommitHalted("IMMUTABLE_FILL_SET_CONFLICT")
                        for fill in fills.fills:
                            trade = await conn.fetchval(
                                INSERT_FILL_SQL, fill.trade_id, fill.order_id,
                                fill.position_id, fill.side, fill.contracts,
                                fill.price, fill.fee_usdt, fill.timestamp_ms,
                                fill.category,
                            )
                            if trade != fill.trade_id:
                                raise ProtectedCommitHalted("IMMUTABLE_FILL_CONFLICT")
                        checkpoint_id = await conn.fetchval(
                            INSERT_CHECKPOINT_SQL,
                            cp.position_id, cp.symbol, cp.external_oid,
                            cp.exchange_order_id, cp.contracts, cp.entry_price,
                            cp.stop_order_id, cp.stop_price,
                            cp.best_profit_pct, cp.armed,
                            cp.lifecycle_state, cp.checkpoint_version,
                            ENTRY_PROTECTED,
                        )
                        if checkpoint_id != cp.position_id:
                            raise ProtectedCommitHalted("CHECKPOINT_INSERT_CONFLICT")
                        revision = await conn.fetchval(
                            INSERT_REVISION_SQL,
                            cp.position_id, cp.checkpoint_version,
                            ENTRY_PROTECTED, cp.symbol, cp.external_oid,
                            cp.exchange_order_id, cp.contracts,
                            cp.entry_price, cp.stop_order_id, cp.stop_price,
                            cp.best_profit_pct, cp.armed, cp.lifecycle_state,
                        )
                        if type(revision) is not int or revision <= 0:
                            raise ProtectedCommitHalted("REVISION_INSERT_FAILED")
                        if await conn.fetchval(
                            SLOT_ACK_SQL, intent.signal_id, intent.external_oid,
                            lease.holder_id, lease.fencing_token,
                        ) != intent.signal_id:
                            raise ProtectedCommitHalted("SLOT_ACK_CAS_FAILED")
                        if await conn.fetchval(
                            SLOT_PROTECTED_SQL, intent.signal_id,
                            intent.external_oid, lease.holder_id,
                            lease.fencing_token,
                        ) != intent.signal_id:
                            raise ProtectedCommitHalted("SLOT_PROTECTED_CAS_FAILED")
            return ProtectedCommitResult(
                "V40_PROTECTED_RECORDS_ATOMICALLY_PERSISTED_NONAUTHORIZING",
                entries_committed=len(batch.entries),
                db_atomic_commit_observed=True,
            )
        except ProtectedCommitHalted:
            raise
        except Exception:
            raise ProtectedCommitHalted("PROTECTED_COMMIT_DB_TRANSACTION_FAILED") from None
