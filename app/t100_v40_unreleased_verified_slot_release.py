"""UNRELEASED single-TX verified native-close -> immutable slot release.

A complete, positively certified native SL terminal/close-fill/survivor
witness plus independently verified and *already persisted* complete
entry+close+funding accounting are prerequisites. No exchange calls, order
POST, stop edit, generic 'missing position' inference or account scope
reset. An account advisory lock and live leader fence serialize every
close/checkpoint/intent/reservation mutation.

The database release is NOT entry authorization. Following commit, the
trader must read an entirely new full-account exchange+ledger bookend and
fresh guardian, risk, and signal witness before the next entry. A DB
timeout after UPDATE is UNKNOWN; caller MUST reconcile, NEVER repeat.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal as D
from typing import Any

from app.t100_live_db_inspect import EXPECTED_DATABASE
from app.t100_live_role_preflight import TARGET_ROLE
from app.t100_live_leader_fencing import LeaderLease
from app.t100_live_durable_recovery import PositionCheckpoint
from app.t100_live_checkpoint_journal import (
    CLOSE_VERIFIED,CLOSING_LATCHED,build_lifecycle_checkpoint,
    validate_checkpoint_transition,PostgresCheckpointJournal,
)
from app.t100_live_four_slot_reservations import ACCOUNT_LOCK_SQL,LEASE_SQL
from app.t100_v40_unreleased_portfolio_native_close import V40PortfolioCloseWitness
from app.t100_live_position_accounting import (
    VerifiedClosedPositionAccounting, PostgresFinalAccountingLedger,
)


class V40SlotReleaseHalted(RuntimeError):
    pass


@dataclass(frozen=True)
class V40SlotReleaseResult:
    status: str
    position_id: str | None = None
    durable_release_and_close_committed: bool = False
    post_commit_signed_get_required: bool = True
    fresh_guardian_and_margin_recheck_required: bool = True
    next_entry_allowed: bool = False
    live_authorization: bool = False
    mexc_posts: int = 0


def _safe_decimal(v: object) -> D:
    if type(v) is D and v.is_finite():
        return v
    raise V40SlotReleaseHalted("V40_CLOSE_ACCOUNTING_NUMBER_INVALID")


def _proofs_match(
    *,
    witness: V40PortfolioCloseWitness,
    previous: PositionCheckpoint,
    accounting: VerifiedClosedPositionAccounting,
    lease: LeaderLease,
    observed_at: datetime,
) -> None:
    if (
        type(witness) is not V40PortfolioCloseWitness
        or type(previous) is not PositionCheckpoint
        or type(accounting) is not VerifiedClosedPositionAccounting
        or type(lease) is not LeaderLease
        or type(observed_at) is not datetime
        or observed_at.tzinfo is None or observed_at.utcoffset() is None
    ):
        raise V40SlotReleaseHalted("V40_CLOSE_POSITIVE_EXCHANGE_OR_ACCOUNTING_EVIDENCE_INVALID")
    if (
        witness.status not in (
            "V40_PORTFOLIO_CLOSE_EXCHANGE_TP_LABEL_CONFLICT_UNRELEASED",
            "V40_PORTFOLIO_CLOSE_EXCHANGE_SL_LABEL_OBSERVED_UNRELEASED",
        )
        or witness.closed_position_id!=previous.position_id
        or witness.exact_closed_position_history_verified is not True
        or witness.portfolio_unchanged_across_bookends is not True
        or type(witness.surviving_protected_slots) is not int
        or not 0<=witness.surviving_protected_slots<=3
        or witness.slot_released is not False
        or witness.live_authorization is not False
        or witness.mexc_posts != 0 or witness.database_writes != 0
        or witness.closing_fills is None
        or witness.closing_fills.final_order_fill_history_complete is not True
        or witness.closing_fills.strategy_fully_filled_order_complete is not True
        or witness.closing_fills.side!="CLOSE_SHORT"
        or witness.closing_fills.position_id!=previous.position_id
        or witness.closing_fills.symbol!=previous.symbol
        or witness.closing_fills.deal_volume!=previous.contracts
        or witness.closing_fills.order_id!=witness.exact_close_order_id
        or witness.native_terminal is None
        or witness.native_terminal.owned_position_closed_verified is not True
        or witness.native_terminal.linked_venue_close_filled is not True
        or witness.native_terminal.native_stop_terminal_verified is not True
        or type(previous) is not PositionCheckpoint
        or previous.lifecycle_state!="OPEN"
        or type(accounting) is not VerifiedClosedPositionAccounting
        or accounting.reconciliation.status!="CLOSED_RECONCILED"
        or accounting.reconciliation.position_id!=previous.position_id
        or accounting.reconciliation.opened_contracts!=previous.contracts
        or accounting.reconciliation.closed_contracts!=previous.contracts
        or accounting.entry_order_id!=previous.exchange_order_id
        or accounting.close_order_id!=witness.exact_close_order_id
        or accounting.stop_order_id!=previous.stop_order_id
        or accounting.fill_history_complete is not True
        or accounting.funding_history_complete is not True
        or accounting.close_provenance_verified is not True
        or accounting.live_authorization is not False
        or type(lease) is not LeaderLease
        or lease.lease_name!="t100-live-executor"
        or type(lease.fencing_token) is not int or lease.fencing_token<=0
        or lease.live_authorization is not False
        or type(observed_at) is not datetime
        or observed_at.tzinfo is None or observed_at.utcoffset() is None
    ):
        raise V40SlotReleaseHalted("V40_CLOSE_POSITIVE_EXCHANGE_OR_ACCOUNTING_EVIDENCE_INVALID")


async def _advance_checkpoint_in_transaction(
    conn: Any, prior: PositionCheckpoint,
    target: PositionCheckpoint, *, reason: str,
) -> None:
    validate_checkpoint_transition(prior,target,reason=reason)
    writer=PostgresCheckpointJournal(None)
    updated=await conn.fetchval(
        """UPDATE public.t100_live_position_checkpoints
           SET stop_order_id=$7,stop_price=$8,best_profit_pct=$9,
               armed=$10,lifecycle_state=$11,checkpoint_version=$12,
               last_write_reason=$13,updated_at=clock_timestamp(),
               verified_at=clock_timestamp()
           WHERE position_id=$1 AND symbol=$2 AND external_oid=$3
             AND exchange_order_id=$4 AND contracts=$5 AND entry_price=$6
             AND checkpoint_version=$14 AND lifecycle_state=$15
           RETURNING position_id""",
        *writer._args(target),reason,prior.checkpoint_version,prior.lifecycle_state,
    )
    if updated!=target.position_id:
        raise V40SlotReleaseHalted("V40_CLOSE_CHECKPOINT_VERSION_CAS_FAILED")
    await writer._append_revision(conn,target,reason)


class PostgresV40VerifiedSlotRelease:
    """Requires the SAME account-wide advisory lock as slot reservation."""

    def __init__(self,pool:Any)->None:
        if pool is None or not callable(getattr(pool,"acquire",None)):
            raise ValueError("V40_RELEASE_RUNTIME_POOL_REQUIRED")
        self.pool=pool

    async def close_and_release_once(
        self, *, witness: V40PortfolioCloseWitness,
        previous: PositionCheckpoint,
        accounting: VerifiedClosedPositionAccounting,
        lease: LeaderLease,
        observed_at: datetime,
    ) -> V40SlotReleaseResult:
        _proofs_match(
            witness=witness,previous=previous,accounting=accounting,
            lease=lease,observed_at=observed_at,
        )
        try:
            closing=build_lifecycle_checkpoint(previous,target="CLOSING")
            closed=build_lifecycle_checkpoint(
                closing,target="CLOSED",position_absent=True,
                stop_absent=True,close_provenance_verified=True,
            )
            async with self.pool.acquire() as conn:
                async with conn.transaction(isolation="read_committed"):
                    await conn.execute(ACCOUNT_LOCK_SQL)
                    if (
                        await conn.fetchval("SELECT current_user")!=TARGET_ROLE
                        or await conn.fetchval("SELECT current_database()")!=EXPECTED_DATABASE
                    ):
                        raise V40SlotReleaseHalted("V40_CLOSE_RUNTIME_DB_ROLE_INVALID")
                    leader=await conn.fetchrow(LEASE_SQL)
                    if (
                        leader is None
                        or leader["holder_id"]!=lease.holder_id
                        or type(leader["fencing_token"]) is not int
                        or leader["fencing_token"]!=lease.fencing_token
                        or leader["unexpired"] is not True
                    ):
                        raise V40SlotReleaseHalted("V40_CLOSE_LEADER_FENCE_STALE")
                    age=await conn.fetchval(
                        "SELECT EXTRACT(EPOCH FROM (clock_timestamp()-$1::timestamptz))",
                        observed_at.astimezone(timezone.utc),
                    )
                    age=D(str(age))
                    if not age.is_finite() or not D("-0.25")<=age<=D("2"):
                        raise V40SlotReleaseHalted("V40_CLOSE_EXCHANGE_WITNESS_STALE")

                    # A finalize-and-release must not race another position
                    # claim or half-finished native-stop mutation.
                    unsettled=await conn.fetchval(
                        """SELECT COUNT(*)::int
                           FROM public.t100_live_order_intents
                           WHERE state IN ('PREPARED','SUBMITTING','UNKNOWN','REVIEW')"""
                    )
                    stops=await conn.fetchval(
                        """SELECT COUNT(*)::int
                           FROM public.t100_live_stop_amendments
                           WHERE state<>'VERIFIED'"""
                    )
                    ambiguous=await conn.fetchval(
                        """SELECT COUNT(*)::int
                           FROM public.t100_live_v40_slot_reservations
                           WHERE state IN ('RESERVED','SUBMITTING','UNKNOWN','ACK','HALTED')"""
                    )
                    if any(type(x) is not int or x!=0
                           for x in (unsettled,stops,ambiguous)):
                        raise V40SlotReleaseHalted("V40_CLOSE_UNRESOLVED_MUTATIONS_PRESENT")

                    active=await conn.fetch(
                        """SELECT position_id,symbol
                           FROM public.t100_live_position_checkpoints
                           WHERE lifecycle_state<>'CLOSED'
                           ORDER BY position_id FOR UPDATE"""
                    )
                    if (
                        type(active) not in (list,tuple)
                        or len(active)!=witness.surviving_protected_slots+1
                        or not 1<=len(active)<=4
                        or sum(str(x["position_id"])==previous.position_id
                               for x in active)!=1
                    ):
                        raise V40SlotReleaseHalted("V40_CLOSE_ACTIVE_PORTFOLIO_TOPOLOGY_DRIFT")
                    row=await conn.fetchrow(
                        """SELECT position_id,symbol,external_oid,exchange_order_id,
                                  contracts,entry_price,stop_order_id,stop_price,
                                  best_profit_pct,armed,lifecycle_state,checkpoint_version
                           FROM public.t100_live_position_checkpoints
                           WHERE position_id=$1 FOR UPDATE""",previous.position_id,
                    )
                    if row is None or any(row[k]!=getattr(previous,k)
                        for k in (
                            "position_id","symbol","external_oid","exchange_order_id",
                            "contracts","entry_price","stop_order_id","stop_price",
                            "best_profit_pct","armed","lifecycle_state",
                            "checkpoint_version",
                        )):
                        raise V40SlotReleaseHalted("V40_CLOSE_CHECKPOINT_IDENTITY_DRIFT")
                    reservation=await conn.fetchrow(
                        """SELECT signal_id,symbol,external_oid,state
                           FROM public.t100_live_v40_slot_reservations
                           WHERE external_oid=$1 FOR UPDATE""",previous.external_oid,
                    )
                    if (
                        reservation is None
                        or reservation["symbol"]!=previous.symbol
                        or reservation["state"]!="PROTECTED"
                        or type(reservation["signal_id"]) is not int
                    ):
                        raise V40SlotReleaseHalted("V40_CLOSE_RESERVED_ORIGIN_UNVERIFIED")

                    # The final ledger writer verifies all economics when
                    # inserting. Recheck the SAME complete immutable row
                    # under the release transaction's account-wide lock.
                    # Matching only net PnL/volume permits changed fees,
                    # funding or notionals to masquerade as final settlement.
                    financial=await conn.fetchrow(
                        """SELECT position_id,entry_order_id,close_order_id,
                                  stop_order_id,opened_contracts,closed_contracts,
                                  open_notional_usdt,close_notional_usdt,
                                  realized_gross_usdt,fees_usdt,
                                  funding_cashflow_usdt,realized_net_usdt
                           FROM public.t100_live_final_position_accounting
                           WHERE position_id=$1 FOR SHARE""",
                        previous.position_id,
                    )
                    expected=PostgresFinalAccountingLedger._expected(accounting)
                    if not PostgresFinalAccountingLedger._same_complete_row(
                        financial, expected,
                    ):
                        raise V40SlotReleaseHalted(
                            "V40_CLOSE_FINAL_ACCOUNTING_NOT_PERSISTED"
                        )

                    await _advance_checkpoint_in_transaction(
                        conn,previous,closing,reason=CLOSING_LATCHED,
                    )
                    await _advance_checkpoint_in_transaction(
                        conn,closing,closed,reason=CLOSE_VERIFIED,
                    )
                    intent=await conn.fetchval(
                        """UPDATE public.t100_live_order_intents
                           SET state='CLOSED',updated_at=clock_timestamp()
                           WHERE external_oid=$1 AND exchange_order_id=$2
                             AND state='ACK' AND symbol=$3
                           RETURNING id""",previous.external_oid,
                        previous.exchange_order_id,previous.symbol,
                    )
                    if type(intent) is not int or intent<=0:
                        raise V40SlotReleaseHalted("V40_CLOSE_INTENT_ACK_CAS_FAILED")
                    released=await conn.fetchval(
                        """UPDATE public.t100_live_v40_slot_reservations
                           SET state='RELEASED',updated_at=clock_timestamp()
                           WHERE external_oid=$1 AND symbol=$2
                             AND state='PROTECTED'
                           RETURNING signal_id""",
                        previous.external_oid,previous.symbol,
                    )
                    if released!=reservation["signal_id"]:
                        raise V40SlotReleaseHalted("V40_CLOSE_RESERVATION_CAS_FAILED")
            return V40SlotReleaseResult(
                "V40_CLOSE_DB_RELEASE_COMMITTED_RESTART_GET_REQUIRED",
                position_id=previous.position_id,
                durable_release_and_close_committed=True,
            )
        except V40SlotReleaseHalted:
            raise
        except Exception:
            # A lost SQL ACK may mean the transaction COMMITTED.
            # Callers must re-read exact checkpoint/accounting/reservation;
            # never send a new order or silently attempt another release.
            raise V40SlotReleaseHalted(
                "V40_CLOSE_SQL_OUTCOME_UNKNOWN_RECONCILE_ONLY"
            ) from None
