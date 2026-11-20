"""Close-only immutable funding and fill custody for an actual V40 native exit.

A native SL can close between bars. Persist the exact independent entry/close
deal history and twice-complete funding in ONE SERIALIZABLE, account-locked,
leader-fenced transaction. This is *not* final PnL certification or permission
to recycle a slot. A matching pre-POST contract multiplier plus the current
one does not prove every historical multiplier in between; the existing
financial reader deliberately marks that continuity unverified.

Never read a candidate, send an exchange POST, amend a stop, reset a latch,
change an account mode, or grant a live entry permit. All ambiguous outcomes
halt for independent SQL/venue reconciliation, with no automatic retry.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from app.t100_live_db_inspect import EXPECTED_DATABASE
from app.t100_live_role_preflight import TARGET_ROLE
from app.t100_live_durable_recovery import (
    PositionCheckpoint,checkpoint_from_row,
)
from app.t100_live_four_slot_ledger_reader import PostgresFourSlotLedgerReader
from app.t100_live_four_slot_snapshot_coordinator import (
    FourSlotSnapshotResult,_ledger_digest,
)
from app.t100_live_leader_fencing import LeaderLease
from app.t100_live_four_slot_reservations import ACCOUNT_LOCK_SQL,LEASE_SQL
from app.t100_live_fill_ledger import VerifiedOrderFillSet
from app.t100_live_funding_ledger import VerifiedPositionFundingSet
from app.t100_v40_native_close_mismatch_recovery import (
    V40NativeCloseMismatchRecovery,V40NativeCloseMismatchEvidence,
)
from app.t100_v40_portfolio_close_financial_reader import (
    V40NativeCloseFinancialReadPort,V40NativeCloseFinancialEvidence,
)


class V40CloseArtifactHalted(RuntimeError):
    """Finite redacted reason; never include raw SQL or MEXC data."""


@dataclass(frozen=True)
class V40CloseArtifactReceipt:
    status: str
    position_id: str
    immutable_entry_fills_verified: bool = False
    immutable_close_fills_verified: bool = False
    immutable_funding_verified: bool = False
    final_accounting_verified: bool = False
    historical_multiplier_continuity_certified: bool = False
    slot_released: bool = False
    next_entry_allowed: bool = False
    live_authorization: bool = False
    mexc_posts: int = 0
    # One SQL TX can insert multiple rows, but status never certifies the
    # individual row count or a completed release.
    database_commit_may_have_occurred: bool = False


def _require_native(
    native:V40NativeCloseMismatchEvidence,
    financial:V40NativeCloseFinancialEvidence,
)->None:
    if (
        type(native) is not V40NativeCloseMismatchEvidence
        or native.status!=
            "V40_EXACT_ONE_NATIVE_EXIT_POSITIVE_ACCOUNTING_AND_RELEASE_PENDING"
        or native.ledger_bookends_equal is not True
        or native.venue_bookends_equal is not True
        or native.positive_native_close_verified is not True
        or native.slot_release_permitted is not False
        or native.next_entry_allowed is not False
        or native.live_authorization is not False
        or native.database_writes!=0 or native.mexc_posts!=0
        or type(native.previous) is not PositionCheckpoint
        or native.previous.lifecycle_state!="OPEN"
        or type(financial) is not V40NativeCloseFinancialEvidence
        or financial.at_entry_contract_size_independently_verified is not True
        or financial.historical_contract_multiplier_certified is not False
        or financial.archived_entry_contract_size is None
        or financial.archived_entry_contract_size!=financial.current_contract_size
        or financial.status!=
           "V40_NATIVE_CLOSE_ECONOMICS_ARCHIVED_ENTRY_MULTIPLIER_MATCHED_HISTORICAL_CONTINUITY_UNCERTIFIED"
        or financial.database_writes!=0 or financial.exchange_posts!=0
        or financial.live_authorization is not False
        or financial.entry_fills.order_id!=native.previous.exchange_order_id
        or financial.entry_fills.position_id!=native.previous.position_id
        or financial.entry_fills.symbol!=native.previous.symbol
        or financial.entry_fills.side!="OPEN_SHORT"
        or financial.close_fills!=native.witness.closing_fills
        or financial.close_fills.order_id!=native.witness.exact_close_order_id
        or financial.close_fills.position_id!=native.previous.position_id
        or financial.close_fills.symbol!=native.previous.symbol
        or financial.close_fills.side!="CLOSE_SHORT"
        or financial.funding.position_id!=native.previous.position_id
        or financial.funding.economics_verified is not True
        or financial.funding.double_collection_finality_verified is not True
        or financial.close_provenance.position_id!=native.previous.position_id
        or financial.close_provenance.stop_order_id!=native.previous.stop_order_id
        or financial.close_provenance.close_order_id!=native.witness.exact_close_order_id
        or financial.accounting.entry_order_id!=native.previous.exchange_order_id
        or financial.accounting.close_order_id!=native.witness.exact_close_order_id
        or financial.accounting.stop_order_id!=native.previous.stop_order_id
        or financial.accounting.reconciliation.position_id!=native.previous.position_id
        or financial.accounting.fill_history_complete is not True
        or financial.accounting.funding_history_complete is not True
    ):
        raise V40CloseArtifactHalted("V40_CLOSE_ARTIFACT_POSITIVE_FINANCIAL_IDENTITY_UNVERIFIED")


async def _persist_fill(conn:Any, item:VerifiedOrderFillSet)->None:
    if (
        type(item) is not VerifiedOrderFillSet
        or item.final_order_fill_history_complete is not True
        or item.strategy_fully_filled_order_complete is not True
        or item.fee_currency!="USDT"
        or not item.fills
    ):
        raise V40CloseArtifactHalted("V40_CLOSE_ARTIFACT_ORDER_FILL_NOT_FINAL")
    await conn.fetchval(
        """INSERT INTO public.t100_live_order_fill_sets
           (order_id,position_id,symbol,side,order_state,
            deal_volume,average_price,fee_currency,fill_count,
            fill_digest_sha256,strategy_fully_filled_order_complete)
           VALUES($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11)
           ON CONFLICT (order_id) DO NOTHING
           RETURNING order_id""",
        item.order_id,item.position_id,item.symbol,item.side,
        item.order_state,item.deal_volume,item.average_price,
        item.fee_currency,len(item.fills),item.fill_digest_sha256,
        item.strategy_fully_filled_order_complete,
    )
    # Require FULL recorded economics, not merely a matching digest and
    # count. This readback is inside the same locked transaction.
    header=await conn.fetchrow(
        """SELECT order_id,position_id,symbol,side,order_state,
                  deal_volume,average_price,fee_currency,fill_count,
                  fill_digest_sha256,strategy_fully_filled_order_complete
           FROM public.t100_live_order_fill_sets WHERE order_id=$1 FOR SHARE""",
        item.order_id,
    )
    expected=(item.order_id,item.position_id,item.symbol,item.side,
              item.order_state,item.deal_volume,item.average_price,
              item.fee_currency,len(item.fills),item.fill_digest_sha256,True)
    keys=("order_id","position_id","symbol","side","order_state",
          "deal_volume","average_price","fee_currency","fill_count",
          "fill_digest_sha256","strategy_fully_filled_order_complete")
    if header is None or tuple(header[k] for k in keys)!=expected:
        raise V40CloseArtifactHalted("V40_CLOSE_ARTIFACT_EXISTING_FILL_HEADER_DRIFT")
    for fill in item.fills:
        inserted=await conn.fetchval(
            """INSERT INTO public.t100_live_order_fills
               (trade_id,order_id,position_id,side,contracts,
                price,fee_usdt,timestamp_ms,category)
               VALUES($1,$2,$3,$4,$5,$6,$7,$8,$9)
               ON CONFLICT (trade_id) DO NOTHING RETURNING trade_id""",
            fill.trade_id,fill.order_id,fill.position_id,fill.side,
            fill.contracts,fill.price,fill.fee_usdt,fill.timestamp_ms,
            fill.category,
        )
        # The global trade_id can belong to a different order; ALWAYS
        # compare every column on readback even after conflict/no insert.
        if inserted is not None and inserted!=fill.trade_id:
            raise V40CloseArtifactHalted("V40_CLOSE_ARTIFACT_FILL_INSERT_IDENTITY_DRIFT")
    actual=await conn.fetch(
        """SELECT trade_id,order_id,position_id,side,contracts,
                  price,fee_usdt,timestamp_ms,category
           FROM public.t100_live_order_fills
           WHERE order_id=$1 ORDER BY timestamp_ms,trade_id""",
        item.order_id,
    )
    if type(actual) not in (list,tuple) or len(actual)!=len(item.fills):
        raise V40CloseArtifactHalted("V40_CLOSE_ARTIFACT_FILL_ROW_COUNT_DRIFT")
    for got,fill in zip(actual,item.fills):
        if tuple(got[k] for k in (
            "trade_id","order_id","position_id","side","contracts",
            "price","fee_usdt","timestamp_ms","category",
        ))!=(
            fill.trade_id,fill.order_id,fill.position_id,fill.side,
            fill.contracts,fill.price,fill.fee_usdt,fill.timestamp_ms,
            fill.category,
        ):
            raise V40CloseArtifactHalted("V40_CLOSE_ARTIFACT_FILL_ROW_IDENTITY_DRIFT")


async def _persist_funding(conn:Any, item:VerifiedPositionFundingSet)->None:
    if (
        type(item) is not VerifiedPositionFundingSet
        or item.economics_verified is not True
        or item.double_collection_finality_verified is not True
        or type(item.total_count) is not int
        or item.total_count!=len(item.events)
    ):
        raise V40CloseArtifactHalted("V40_CLOSE_ARTIFACT_FUNDING_NOT_FINAL")
    await conn.fetchval(
        """INSERT INTO public.t100_live_position_funding_sets
           (position_id,total_count,funding_digest_sha256,
            economics_verified,double_collection_finality_verified)
           VALUES($1,$2,$3,$4,$5)
           ON CONFLICT (position_id) DO NOTHING RETURNING position_id""",
        item.position_id,item.total_count,item.funding_digest_sha256,
        True,True,
    )
    header=await conn.fetchrow(
        """SELECT position_id,total_count,funding_digest_sha256,
                  economics_verified,double_collection_finality_verified
           FROM public.t100_live_position_funding_sets
           WHERE position_id=$1 FOR SHARE""",item.position_id,
    )
    if (
        header is None or
        tuple(header[k] for k in (
            "position_id","total_count","funding_digest_sha256",
            "economics_verified","double_collection_finality_verified"
        ))!=(item.position_id,item.total_count,item.funding_digest_sha256,True,True)
    ):
        raise V40CloseArtifactHalted("V40_CLOSE_ARTIFACT_FUNDING_HEADER_DRIFT")
    for event in item.events:
        inserted=await conn.fetchval(
            """INSERT INTO public.t100_live_funding_events
               (event_id,position_id,symbol,position_type,
                position_value,funding_cashflow_usdt,rate,settle_time_ms)
               VALUES($1,$2,$3,$4,$5,$6,$7,$8)
               ON CONFLICT (event_id) DO NOTHING RETURNING event_id""",
            event.event_id,event.position_id,event.symbol,
            event.position_type,event.position_value,
            event.funding_cashflow_usdt,event.rate,event.settle_time_ms,
        )
        if inserted is not None and inserted!=event.event_id:
            raise V40CloseArtifactHalted("V40_CLOSE_ARTIFACT_FUNDING_INSERT_IDENTITY_DRIFT")
    actual=await conn.fetch(
        """SELECT event_id,position_id,symbol,position_type,position_value,
                  funding_cashflow_usdt,rate,settle_time_ms
           FROM public.t100_live_funding_events
           WHERE position_id=$1 ORDER BY settle_time_ms,event_id""",
        item.position_id,
    )
    expected=sorted(item.events,key=lambda x:(x.settle_time_ms,x.event_id))
    if type(actual) not in (list,tuple) or len(actual)!=len(expected):
        raise V40CloseArtifactHalted("V40_CLOSE_ARTIFACT_FUNDING_ROW_COUNT_DRIFT")
    for got,event in zip(actual,expected):
        if tuple(got[k] for k in (
            "event_id","position_id","symbol","position_type",
            "position_value","funding_cashflow_usdt","rate","settle_time_ms",
        ))!=(
            event.event_id,event.position_id,event.symbol,event.position_type,
            event.position_value,event.funding_cashflow_usdt,event.rate,
            event.settle_time_ms,
        ):
            raise V40CloseArtifactHalted("V40_CLOSE_ARTIFACT_FUNDING_ROW_IDENTITY_DRIFT")


class V40CloseOnlyFinancialArtifactStaging:
    """Same-account signed GET and restricted SQL role, no executor POST path."""

    def __init__(
        self, *,
        native:V40NativeCloseMismatchRecovery,
        finance:V40NativeCloseFinancialReadPort,
        ledger:PostgresFourSlotLedgerReader,
        writer_pool:Any,
    )->None:
        if (
            type(native) is not V40NativeCloseMismatchRecovery
            or type(finance) is not V40NativeCloseFinancialReadPort
            or type(ledger) is not PostgresFourSlotLedgerReader
            or native.ledger is not ledger
            or native.probe is not finance.probe
            or native.venue.probe is not finance.probe
            or finance.entry_contracts is None
            or finance.entry_contracts.pool is not ledger.pool
            or writer_pool is None
            or writer_pool is ledger.pool
            or not callable(getattr(writer_pool,"acquire",None))
        ):
            raise V40CloseArtifactHalted("V40_CLOSE_ARTIFACT_SAME_ACCOUNT_PROBE_AND_SQL_REQUIRED")
        self.native=native
        self.finance=finance
        self.ledger=ledger
        # The owner/reader pool is SELECT-only. Never reuse it for INSERT.
        # The independent executor pool must have the write-capable runtime
        # role and explicit read-write transaction semantics.
        self.writer_pool=writer_pool

    async def stage_once(
        self, *, snapshot:FourSlotSnapshotResult,
        lease:LeaderLease,
    )->V40CloseArtifactReceipt:
        if (
            type(lease) is not LeaderLease
            or lease.lease_name!="t100-live-executor"
            or type(lease.holder_id) is not str or not lease.holder_id
            or type(lease.fencing_token) is not int or lease.fencing_token<=0
            or lease.live_authorization is not False
        ):
            raise V40CloseArtifactHalted("V40_CLOSE_ARTIFACT_FRESH_LEADER_REQUIRED")
        try:
            native=await self.native.observe(snapshot)
            financial=await self.finance.observe(
                previous=native.previous,witness=native.witness,
            )
            _require_native(native,financial)
            # REPEATABLE READ restricted SQL source independently checks
            # the same OPEN checkpoint identities before any writes.
            observed=await self.ledger.read_snapshot()
            checkpoints=tuple(checkpoint_from_row(x) for x in observed.checkpoints)
            if (
                len(checkpoints)!=len(native.survivors)+1
                or {c.position_id for c in checkpoints}!=
                  {native.previous.position_id}|{c.position_id for c in native.survivors}
                or native.previous not in checkpoints
                or any(c not in checkpoints for c in native.survivors)
            ):
                raise V40CloseArtifactHalted(
                    "V40_CLOSE_ARTIFACT_OPEN_LEDGER_CHANGED_AFTER_GET"
                )
            ledger_hash=_ledger_digest(observed)
            async with self.writer_pool.acquire() as conn:
                async with conn.transaction(isolation="serializable"):
                    await conn.execute(ACCOUNT_LOCK_SQL)
                    if (
                        await conn.fetchval("SELECT current_database()")!=EXPECTED_DATABASE
                        or await conn.fetchval("SELECT current_user")!=TARGET_ROLE
                        or await conn.fetchval(
                            "SELECT current_setting('transaction_read_only')"
                        )!="off"
                    ):
                        raise V40CloseArtifactHalted("V40_CLOSE_ARTIFACT_WRITER_ROLE_INVALID")
                    row=await conn.fetchrow(LEASE_SQL)
                    if (
                        row is None
                        or row["holder_id"]!=lease.holder_id
                        or type(row["fencing_token"]) is not int
                        or row["fencing_token"]!=lease.fencing_token
                        or row["unexpired"] is not True
                    ):
                        raise V40CloseArtifactHalted("V40_CLOSE_ARTIFACT_LEASE_EXPIRED")
                    # Repeat the immutable OPEN origin inside the write TX.
                    current=await conn.fetchrow(
                        """SELECT symbol,external_oid,exchange_order_id,
                                  position_id,contracts,entry_price,stop_order_id,
                                  stop_price,best_profit_pct,armed,lifecycle_state,
                                  checkpoint_version
                           FROM public.t100_live_position_checkpoints
                           WHERE position_id=$1 FOR SHARE""",
                        native.previous.position_id,
                    )
                    if (
                        current is None
                        or checkpoint_from_row(dict(current))!=native.previous
                    ):
                        raise V40CloseArtifactHalted(
                            "V40_CLOSE_ARTIFACT_LOCKED_CHECKPOINT_DRIFT"
                        )
                    unsettled=await conn.fetchval(
                        """SELECT COUNT(*)::int FROM public.t100_live_order_intents
                           WHERE state IN ('PREPARED','SUBMITTING','UNKNOWN','REVIEW')"""
                    )
                    stop_pending=await conn.fetchval(
                        """SELECT COUNT(*)::int FROM public.t100_live_stop_amendments
                           WHERE state<>'VERIFIED'"""
                    )
                    if (
                        type(unsettled) is not int or unsettled!=0
                        or type(stop_pending) is not int or stop_pending!=0
                    ):
                        raise V40CloseArtifactHalted(
                            "V40_CLOSE_ARTIFACT_UNRESOLVED_MUTATION"
                        )
                    await _persist_fill(conn,financial.entry_fills)
                    await _persist_fill(conn,financial.close_fills)
                    await _persist_funding(conn,financial.funding)
            # No final P&L write: an archived multiplier captured before
            # the entry and the current multiplier alone cannot certify all
            # historical contract changes between the two timestamps.
            # Finalization/slot release must use a separate strong proof.
            return V40CloseArtifactReceipt(
                status="V40_CLOSE_FILLS_AND_FUNDING_DURABLY_STAGED_FINAL_ACCOUNTING_NOT_CERTIFIED",
                position_id=native.previous.position_id,
                immutable_entry_fills_verified=True,
                immutable_close_fills_verified=True,
                immutable_funding_verified=True,
                database_commit_may_have_occurred=True,
            )
        except V40CloseArtifactHalted:
            raise
        except BaseException:
            # COMMIT acknowledgement may have been lost; never retry based
            # on this status. Reinspect exact immutable rows and MEXC first.
            raise V40CloseArtifactHalted(
                "V40_CLOSE_ARTIFACT_SQL_OR_SIGNED_GET_OUTCOME_UNKNOWN_RECONCILE_ONLY"
            ) from None
