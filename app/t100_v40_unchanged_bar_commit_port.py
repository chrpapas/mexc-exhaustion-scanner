"""Atomic V40 unchanged-native-stop completed-Min30 bar cursor writer.

This is the real DB mutation port for the V40 four-slot supervisor. It
re-observes a NEW independent signed full-account + native-SL inventory,
restricted scanner bars, immutable ledger, per-position cursors, and scope
guardian before entering an account-locked, leader-fenced SQL transaction.

A batch of 1..4 unchanged-stop bars is staged, optional best-price
checkpoint revisions appended, and committed atomically. A crash or lost
COMMIT ACK must HALT, never retry optimistically or advance to another
trade. This component does not place an exchange order or issue permits.

It needs a DISTINCT write-capable runtime connection pool; the read-only
Render worker's existing restricted pool cannot be repurposed by flags.
MEXC's GETs are non-atomic with PostgreSQL; operator must still certify
native stop/exit behavior before enabling real orders.
"""
from __future__ import annotations

from datetime import datetime,timezone
from decimal import Decimal as D
from typing import Any,Awaitable,Callable

from app.t100_live_authorization import (
    LiveMutationPermit,V40_PORTFOLIO_SCOPE,
    require_certified_transport_permit,
)
from app.t100_live_checkpoint_journal import (
    BAR_OBSERVED,build_verified_bar_checkpoint,
)
from app.t100_live_db_inspect import EXPECTED_DATABASE
from app.t100_live_durable_recovery import (
    PositionCheckpoint,checkpoint_from_row,
)
from app.t100_live_four_slot_ledger_reader import (
    CHECKPOINTS_SQL,REVISIONS_SQL,INTENTS_SQL,AMENDMENTS_SQL,
    PostgresFourSlotLedgerReader,
)
from app.t100_live_four_slot_snapshot_coordinator import (
    LedgerSnapshot,_ledger_digest,_venue_digest,
)
from app.t100_live_leader_fencing import LeaderLease
from app.t100_live_role_preflight import TARGET_ROLE
from app.t100_live_four_slot_reservations import (
    ACCOUNT_LOCK_SQL,LEASE_SQL,
    UNRESOLVED_INTENTS_SQL,UNRESOLVED_STOPS_SQL,
)
from app.t100_live_native_stop_execution_audit import (
    assess_native_short_market_stop_row,
)
from app.t100_v40_unreleased_bar_cursor import V40DurableBarCursor
from app.t100_v40_unreleased_exit_policy import V40ExitCheckpoint
from app.t100_v40_unreleased_verified_exit_handoff import (
    V40VerifiedExitProjection,
)
from app.t100_v40_live_portfolio_supervisor import (
    V40PortfolioCycleFrame,V40PortfolioSupervisor,
    V40CursorAdvanceResult,
)
from app.t100_v40_unreleased_verified_slot_release import (
    _advance_checkpoint_in_transaction,
)
from app.t100_live_four_slot_mexc_readonly import FourSlotMexcReadVenue


class V40UnchangedCursorCommitHalted(RuntimeError):
    """Redacted fixed code; DB commit may already have occurred."""


async def _read_locked_ledger(conn:Any)->LedgerSnapshot:
    tables=[]
    for sql in (CHECKPOINTS_SQL,REVISIONS_SQL,INTENTS_SQL,AMENDMENTS_SQL):
        rows=await conn.fetch(sql)
        if type(rows) not in (tuple,list):
            raise V40UnchangedCursorCommitHalted(
                "V40_CURSOR_LOCKED_LEDGER_RESPONSE_INVALID"
            )
        tables.append([dict(row) for row in rows])
    return LedgerSnapshot(*tables,readonly_repeatable_read=True)


class PostgresV40UnchangedBarCommit:
    def __init__(
        self, *,
        writer_pool:Any,
        reader:PostgresFourSlotLedgerReader,
        venue:FourSlotMexcReadVenue,
        fresh_observe:Callable[[],Awaitable[V40PortfolioCycleFrame]],
        fresh_lease:Callable[[],Awaitable[LeaderLease]],
    )->None:
        if (
            writer_pool is None
            or not callable(getattr(writer_pool,"acquire",None))
            or type(reader) is not PostgresFourSlotLedgerReader
            or type(venue) is not FourSlotMexcReadVenue
            or not callable(fresh_observe)
            or not callable(fresh_lease)
            or writer_pool is reader.pool
        ):
            raise V40UnchangedCursorCommitHalted(
                "V40_CURSOR_SEPARATE_FENCED_WRITER_AND_RESTRICTED_READER_REQUIRED"
            )
        self.writer_pool=writer_pool
        self.reader=reader
        self.venue=venue
        self.fresh_observe=fresh_observe
        self.fresh_lease=fresh_lease

    @staticmethod
    def _validate_permit(permit:LiveMutationPermit)->None:
        try:
            approved=require_certified_transport_permit(permit)
            if approved.scope!=V40_PORTFOLIO_SCOPE:
                raise ValueError()
        except Exception:
            raise V40UnchangedCursorCommitHalted(
                "V40_CURSOR_CERTIFIED_FOUR_SLOT_PERMIT_REQUIRED"
            ) from None

    async def commit(
        self,projection:V40VerifiedExitProjection, *,
        permit:LiveMutationPermit,
    )->V40CursorAdvanceResult:
        # A database cursor commit is a production strategy mutation;
        # never perform even a GET on the current uncertified build.
        self._validate_permit(permit)
        if (
            type(projection) is not V40VerifiedExitProjection
            or projection.status!=
                "V40_VERIFIED_OWNED_ONE_TO_FOUR_EXITS_PROJECTED_NONAUTHORIZING"
            or projection.verified_exchange_and_ledger_bookends is not True
            or projection.verified_durable_bar_cursors is not True
            or projection.live_execution_certified is not False
            or projection.exchange_posts!=0 or projection.database_writes!=0
            or not 1<=len(projection.batch.slots)<=4
            or projection.batch.unchanged_verified_stop_count
                !=len(projection.batch.slots)
            or projection.batch.native_close_reconciliation_count!=0
            or projection.batch.proposed_stop_amendment_count!=0
            or projection.native_close_reconciliation_count!=0
            or projection.candidate_native_stop_amendment_count!=0
            or any(
                s.decision.status!="KEEP_VERIFIED_NATIVE_STOP"
                or s.requires_native_order_reconciliation
                or s.requires_native_stop_amendment
                or s.decision.proposed_checkpoint is None
                for s in projection.batch.slots
            )
            or len({s.position_id for s in projection.batch.slots})
                !=len(projection.batch.slots)
        ):
            raise V40UnchangedCursorCommitHalted(
                "V40_CURSOR_ONLY_COMPLETE_UNCHANGED_BARS_ALLOWED"
            )
        # Re-observe; never write based on a stale projection from the
        # preceding supervisor iteration or a previously captured candle.
        try:
            frame=await self.fresh_observe()
            lease=await self.fresh_lease()
            if (
                type(frame) is not V40PortfolioCycleFrame
                or not V40PortfolioSupervisor._frame_valid(frame)
                or frame.completed_exit!=projection
                or frame.snapshot.occupied_slots
                    !=projection.protected_position_count
                or frame.snapshot.status
                    !="NONFLAT_PROVENANCE_MATCHED_NONAUTHORIZING"
                or type(frame.snapshot.ledger_hash) is not str
                or len(frame.snapshot.ledger_hash)!=64
                or type(frame.snapshot.critical_inventory_hash) is not str
                or len(frame.snapshot.critical_inventory_hash)!=64
                or type(lease) is not LeaderLease
                or lease.lease_name!="t100-live-executor"
                or type(lease.fencing_token) is not int
                or lease.fencing_token<=0
                or lease.live_authorization is not False
                or _venue_digest(
                    self.venue.last_positions,self.venue.last_stops,
                )!=frame.snapshot.critical_inventory_hash
            ):
                raise V40UnchangedCursorCommitHalted(
                    "V40_CURSOR_CURRENT_SIGNED_LEDGER_GUARDIAN_OR_LEASE_INVALID"
                )
            stops=list(self.venue.last_stops)
            if len(stops)!=frame.snapshot.occupied_slots:
                raise V40UnchangedCursorCommitHalted(
                    "V40_CURSOR_ALL_NATIVE_STOPS_REQUIRED"
                )
            # Independent source and venue bookends in fresh_observe have
            # already verified the account; do not assert venue atomicity.
            signed_at=datetime.now(timezone.utc)
        except V40UnchangedCursorCommitHalted:
            raise
        except Exception:
            raise V40UnchangedCursorCommitHalted(
                "V40_CURSOR_FRESH_EXCHANGE_OR_SQL_PROOF_UNAVAILABLE"
            ) from None

        try:
            committed_ids=[]
            async with self.writer_pool.acquire() as conn:
                # One transaction for ALL due cursors and BAR revisions.
                async with conn.transaction(isolation="serializable"):
                    await conn.execute(ACCOUNT_LOCK_SQL)
                    if (
                        await conn.fetchval("SELECT current_user")!=TARGET_ROLE
                        or await conn.fetchval("SELECT current_database()")
                            !=EXPECTED_DATABASE
                        or await conn.fetchval(
                            "SELECT current_setting('transaction_read_only')"
                        )!="off"
                    ):
                        raise V40UnchangedCursorCommitHalted(
                            "V40_CURSOR_WRITER_ROLE_OR_DB_UNVERIFIED"
                        )
                    leader=await conn.fetchrow(LEASE_SQL)
                    if (
                        leader is None
                        or leader["holder_id"]!=lease.holder_id
                        or leader["fencing_token"]!=lease.fencing_token
                        or leader["unexpired"] is not True
                    ):
                        raise V40UnchangedCursorCommitHalted(
                            "V40_CURSOR_WRITER_FENCE_STALE"
                        )
                    age=await conn.fetchval(
                        """SELECT EXTRACT(EPOCH FROM
                           (clock_timestamp()-$1::timestamptz))""",
                        signed_at,
                    )
                    age=D(str(age))
                    if not age.is_finite() or not D("-0.25")<=age<=D("2"):
                        raise V40UnchangedCursorCommitHalted(
                            "V40_CURSOR_SIGNED_GET_BOOKEND_TOO_OLD"
                        )
                    if _ledger_digest(await _read_locked_ledger(conn))!=(
                        frame.snapshot.ledger_hash
                    ):
                        raise V40UnchangedCursorCommitHalted(
                            "V40_CURSOR_LOCKED_LEDGER_CHANGED"
                        )
                    unsettled=await conn.fetchval(UNRESOLVED_INTENTS_SQL)
                    pending_stops=await conn.fetchval(UNRESOLVED_STOPS_SQL)
                    pending_reservations=await conn.fetchval(
                        """SELECT count(*)::int FROM
                           public.t100_live_v40_slot_reservations
                           WHERE state IN
                           ('RESERVED','SUBMITTING','UNKNOWN','ACK','HALTED')"""
                    )
                    if any(type(n) is not int or n!=0 for n in (
                        unsettled,pending_stops,pending_reservations,
                    )):
                        raise V40UnchangedCursorCommitHalted(
                            "V40_CURSOR_OTHER_UNRESOLVED_MUTATIONS"
                        )
                    active=await conn.fetch(
                        """SELECT position_id,symbol
                           FROM public.t100_live_position_checkpoints
                           WHERE lifecycle_state<>'CLOSED'
                           ORDER BY position_id FOR UPDATE"""
                    )
                    recovered=frame.snapshot.recovery.recovered
                    if (
                        type(active) not in (tuple,list)
                        or len(active)!=len(recovered)
                        or {(str(x["position_id"]),x["symbol"]) for x in active}
                            !={(x.position_id,x.symbol) for x in recovered}
                    ):
                        raise V40UnchangedCursorCommitHalted(
                            "V40_CURSOR_LOCKED_OWNER_SET_DRIFT"
                        )
                    # No two bars for the same position in one cycle.
                    cursor_writer=V40DurableBarCursor(conn)
                    checkpoint_rows={
                        str(x["position_id"]):x
                        for x in (await _read_locked_ledger(conn)).checkpoints
                    }
                    by_stop={
                        str(s["positionId"]):s for s in stops
                        if type(s) is dict and s.get("positionId") is not None
                    }
                    if len(by_stop)!=len(stops):
                        raise V40UnchangedCursorCommitHalted(
                            "V40_CURSOR_DUPLICATE_EXCHANGE_STOP_IDS"
                        )
                    for review in projection.batch.slots:
                        cp=checkpoint_from_row(
                            checkpoint_rows[review.position_id]
                        )
                        cursor=await cursor_writer.read(cp.position_id)
                        decision=review.decision
                        proposed=decision.proposed_checkpoint
                        previous=V40ExitCheckpoint(
                            position_id=cp.position_id,
                            symbol=cp.symbol,entry_price=cp.entry_price,
                            filled_at_ts=(cursor.signed_fill_at_ms+999)//1000,
                            native_stop_price=cp.stop_price,
                            best_profit_pct=cp.best_profit_pct,
                            trail_armed=cp.armed,
                            last_completed_open_ts=cursor.last_committed_open_ts,
                        )
                        if (
                            cp.lifecycle_state!="OPEN"
                            or cp.position_id!=review.position_id
                            or cp.symbol!=review.symbol
                            or proposed is None
                            or proposed.native_stop_price!=cp.stop_price
                            or proposed.best_profit_pct<cp.best_profit_pct
                            or proposed.trail_armed is not cp.armed
                            or cursor.entry_external_oid!=cp.external_oid
                            or cursor.checkpoint_version!=cp.checkpoint_version
                            or cursor.stop_order_id!=cp.stop_order_id
                            or cursor.confirmed_stop_price!=cp.stop_price
                            or cursor.best_profit_pct!=cp.best_profit_pct
                            or cursor.trail_armed is not cp.armed
                            or cursor.pending_open_ts is not None
                            or cursor.position_id!=cp.position_id
                        ):
                            raise V40UnchangedCursorCommitHalted(
                                "V40_CURSOR_CHECKPOINT_OR_COMPLETED_BAR_DRIFT"
                            )
                        sl=by_stop.get(cp.position_id)
                        if sl is None:
                            raise V40UnchangedCursorCommitHalted(
                                "V40_CURSOR_NATIVE_SL_MISSING"
                            )
                        proof=assess_native_short_market_stop_row(
                            stop=sl,symbol=cp.symbol,
                            position_id=cp.position_id,
                            position_quantity=cp.contracts,
                            maximum_short_stop_price=cp.stop_price,
                            verified_filled_entry_order_id=cp.exchange_order_id,
                        )
                        if proof.active_native_full_volume_market_sl_observed is not True:
                            raise V40UnchangedCursorCommitHalted(
                                "V40_CURSOR_SIGNED_NATIVE_SL_NOT_FULL_COVER"
                            )
                        # Stage and commit each BAR under the same atomic
                        # SQL transaction, so no partial 1..4-slot mutation
                        # survives a validation, CAS or guardian failure.
                        await cursor_writer.stage(
                            cursor=cursor,previous=previous,plan=decision,
                        )
                        target=cp
                        if proposed.best_profit_pct>cp.best_profit_pct:
                            target=build_verified_bar_checkpoint(
                                cp,stop=sl,
                                best_profit_pct=proposed.best_profit_pct,
                            )
                            await _advance_checkpoint_in_transaction(
                                conn,cp,target,reason=BAR_OBSERVED,
                            )
                        pending=await cursor_writer.read(cp.position_id)
                        updated=await cursor_writer.commit_verified(
                            cursor=pending,plan=decision,
                            persisted_checkpoint=target,
                            independent_native_sl_get_verified=True,
                            immutable_checkpoint_history_verified=True,
                        )
                        if (
                            updated.position_id!=cp.position_id
                            or updated.last_committed_open_ts
                                !=proposed.last_completed_open_ts
                            or updated.pending_open_ts is not None
                            or updated.checkpoint_version
                                !=target.checkpoint_version
                            or updated.best_profit_pct!=target.best_profit_pct
                        ):
                            raise V40UnchangedCursorCommitHalted(
                                "V40_CURSOR_ATOMIC_COMMIT_READBACK_INVALID"
                            )
                        committed_ids.append(cp.position_id)
            return V40CursorAdvanceResult(
                "V40_UNCHANGED_NATIVE_STOP_BAR_CURSOR_COMMITTED",
                position_ids=tuple(committed_ids),
                durable_commit_verified=True,
                stop_bookends_verified=True,
            )
        except V40UnchangedCursorCommitHalted:
            raise
        except BaseException:
            # A commit ACK may be lost even if the transaction completed.
            # No automatic cursor stage/replay; fresh DB+GET reconciliation
            # must precede *any* future action or new entry.
            raise V40UnchangedCursorCommitHalted(
                "V40_CURSOR_SQL_COMMIT_OUTCOME_UNKNOWN_NO_RETRY"
            ) from None
