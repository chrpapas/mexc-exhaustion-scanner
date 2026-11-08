"""V40 post-dispatch protected record finalization, no order transport.

Bridges existing irreversible-claim GET evidence to the atomic PostgreSQL
ACK/fills/checkpoint writer. It NEVER submits an entry, resubmits a missing
order, places or changes a stop, repairs an unknown outcome or authorizes
live trading. A failed venue GET or DB commit is RECONCILIATION ONLY.

Do not wire to a real account until runtime DB migrations, restricted
privileges, transport, role/fencing, release gates and post-commit restart
witnesses have been independently reviewed. The native exchange and SQL
commit cannot form an atomic distributed transaction.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any, Protocol, Sequence

from app.t100_live_execution import EntryIntent
from app.t100_live_four_slot_attempt_journal import DurableAttemptStage
from app.t100_live_four_slot_snapshot_coordinator import FourSlotSnapshotResult
from app.t100_live_four_slot_protected_get_evidence import (
    ProtectedGetPreparation, ProtectedClaimReader, ProtectedGetVenue,
    ProtectedGetCompleteness, observe_claimed_staged_protection_get_only,
)
from app.t100_live_leader_fencing import LeaderLease
from app.t100_live_v40_protected_commit import ProtectedCommitResult
from app.t100_live_v40_protected_finalization import PreparedProtectedBatch


@dataclass(frozen=True)
class V40PostDispatchReport:
    status: str
    prepared_entry_count: int = 0
    records_committed: int = 0
    claim_bound_get_verified: bool = False
    post_commit_restart_verified: bool = False
    exchange_atomicity_verified: bool = False
    reconcile_only: bool = True
    resubmission_allowed: bool = False
    entry_allowed: bool = False
    live_authorization: bool = False
    mexc_orders_sent: bool = False


class ClaimedProtectedCommit(Protocol):
    async def persist_claimed(
        self, *, batch: PreparedProtectedBatch,
        pre_attempt_witness: FourSlotSnapshotResult,
        lease: LeaderLease, observed_at: datetime,
        shared_pre_positions: list[dict[str, Any]] | None = None,
        shared_pre_stops: list[dict[str, Any]] | None = None,
    ) -> ProtectedCommitResult: ...


async def reconcile_and_commit_protected_v40(
    *, stages: Sequence[DurableAttemptStage],
    intents: Sequence[EntryIntent],
    pre_attempt_witness: FourSlotSnapshotResult,
    claim_reader: ProtectedClaimReader,
    venue: ProtectedGetVenue,
    completeness: ProtectedGetCompleteness,
    commit_writer: ClaimedProtectedCommit,
    lease: LeaderLease,
    observed_at: datetime,
    shared_pre_positions: list[dict[str, Any]] | None = None,
    shared_pre_stops: list[dict[str, Any]] | None = None,
) -> V40PostDispatchReport:
    """One shot GET/SQL path, never starts/resubmits a MEXC order."""
    def block(code: str, **kwargs: Any) -> V40PostDispatchReport:
        return V40PostDispatchReport(status=code, **kwargs)

    if (
        type(stages) not in (tuple, list)
        or type(intents) not in (tuple, list)
        or not 1 <= len(stages) == len(intents) <= 4
        or any(type(s) is not DurableAttemptStage for s in stages)
        or any(type(i) is not EntryIntent for i in intents)
        or type(pre_attempt_witness) is not FourSlotSnapshotResult
        or type(lease) is not LeaderLease
        or lease.lease_name != "t100-live-executor"
        or type(lease.fencing_token) is not int
        or lease.fencing_token <= 0
        or type(observed_at) is not datetime
        or observed_at.tzinfo is None
        or observed_at.utcoffset() is None
        or not all(
            s.signal_id == i.signal_id
            and s.external_oid == i.external_oid
            and s.state == "SUBMITTING"
            and s.post_attempted is False
            and s.reconciliation_required is True
            and s.entry_allowed is False
            and s.live_authorization is False
            for s, i in zip(stages, intents)
        )
    ):
        return block("V40_POST_DISPATCH_INPUT_INVALID_NO_RETRY")
    try:
        get = await observe_claimed_staged_protection_get_only(
            stages=stages, intents=intents,
            pre_attempt_witness=pre_attempt_witness,
            claim_reader=claim_reader, venue=venue, completeness=completeness,
        )
    except BaseException:
        return block("V40_POST_DISPATCH_GET_FAILED_NO_RETRY")
    if (
        type(get) is not ProtectedGetPreparation
        or get.status != "PROTECTED_GET_BATCH_PREPARED_NONAUTHORIZING"
        or get.dispatch_claims_verified is not True
        or get.two_bookends_equal is not True
        or get.prepared_batch is None
        or type(get.prepared_batch) is not PreparedProtectedBatch
        or get.read_only is not True
        or get.live_authorization is not False
        or get.entry_allowed is not False
        or get.mexc_orders_sent is not False
        or get.durable_ack_persisted is not False
        or get.durable_checkpoint_persisted is not False
    ):
        return block("V40_POST_DISPATCH_PROTECTION_GET_NOT_CERTIFIED_NO_RETRY")
    batch = get.prepared_batch
    if (
        batch.status != "PROTECTED_BATCH_PREPARED_OFFLINE_ONLY"
        or batch.durable_ack_persisted is not False
        or batch.durable_checkpoint_persisted is not False
        or batch.mexc_orders_sent is not False
        or batch.live_authorization is not False
        or len(batch.entries) != len(stages)
        or any(
            item.stage != stage
            or item.intent != intent
            for item, stage, intent in zip(batch.entries, stages, intents)
        )
    ):
        return block("V40_POST_DISPATCH_PREPARED_ENTRY_DRIFT_NO_RETRY")

    try:
        commit = await commit_writer.persist_claimed(
            batch=batch, pre_attempt_witness=pre_attempt_witness,
            lease=lease, observed_at=observed_at,
            shared_pre_positions=shared_pre_positions,
            shared_pre_stops=shared_pre_stops,
        )
    except BaseException:
        # An interrupted DB response may mean the transaction committed.
        # Never run it twice by reflex: inspect restricted SQL + venue.
        return block(
            "V40_POST_DISPATCH_COMMIT_UNKNOWN_MANUAL_RECONCILE_ONLY",
            prepared_entry_count=len(batch.entries),
            claim_bound_get_verified=True,
        )
    if (
        type(commit) is not ProtectedCommitResult
        or commit.status
        != "V40_PROTECTED_RECORDS_ATOMICALLY_PERSISTED_NONAUTHORIZING"
        or commit.db_atomic_commit_observed is not True
        or type(commit.entries_committed) is not int
        or commit.entries_committed != len(batch.entries)
        or commit.venue_state_currently_verified is not False
        or commit.exchange_atomicity_verified is not False
        or commit.real_post_attempted is not False
        or commit.entry_allowed is not False
        or commit.live_authorization is not False
        or commit.mexc_orders_sent is not False
    ):
        return block(
            "V40_POST_DISPATCH_COMMIT_RESPONSE_UNVERIFIED_RECHECK_SQL",
            prepared_entry_count=len(batch.entries),
            claim_bound_get_verified=True,
        )
    return V40PostDispatchReport(
        status="V40_PROTECTED_FILL_AND_CHECKPOINT_PERSISTED_RESTART_REQUIRED",
        prepared_entry_count=len(batch.entries),
        records_committed=commit.entries_committed,
        claim_bound_get_verified=True,
    )
