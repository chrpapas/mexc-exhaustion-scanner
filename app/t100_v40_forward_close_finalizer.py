"""Forward-live V40 exact native-close financial finalizer (CLOSE ONLY).

This closes the gap between already-implemented independently positive native
exit, fully collected immutable entry/close fills and funding, and atomic
fenced SQL slot release. It deliberately does not authorize any OPEN order,
amend any native stop, create an execution permit, or proclaim historical
strategy parity. To use it a caller must already control a fenced dedicated
executor-role pool and the exact SAME signed GET account as the portfolio.

Production-forward rule: the contract multiplier archived *before entry* must
equal the current independently read multiplier. That is an observable
bookend for the real forward trial, NOT a statement that undocumented
historical multiplier continuity has been proven. Position P&L is derived
only from actual MEXC deal fills, USDT fees and complete funding events.
No fabricated STOP_LOSS label is needed on MEXC TP/SL-label conflict.

Ordering:
  1. positive exact signed native exit from open checkpoint mismatch;
  2. twice-complete financial GETs and at-entry contract archive;
  3. one SERIALIZABLE, fenced SQL transaction persists immutable artifacts;
  4. NEW independent GET and READ-ONLY SQL confirms same exact hashes;
  5. persist final accounting, refusing changed historical financial hashes;
  6. NEW full native-close signed GET and leader-fenced atomic CLOSED /
     ACK / reservation RELEASE CAS, requiring persisted final accounting.
At any UNKNOWN, stop; never repeat a DB commit or place an exchange order.
After a confirmed release, independent full-account GET, guardian, margin
and signal re-observation is mandatory before ANY new entry.
"""
from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal as D
from typing import Any

from app.t100_live_db_inspect import EXPECTED_DATABASE
from app.t100_live_leader_fencing import LeaderLease
from app.t100_live_owned_emergency_guardian import OwnedShort
from app.t100_live_position_accounting import PostgresFinalAccountingLedger
from app.t100_live_role_preflight import TARGET_ROLE
from app.t100_live_four_slot_snapshot_coordinator import FourSlotSnapshotResult
from app.t100_v40_close_financial_artifact_staging import (
    V40CloseOnlyFinancialArtifactStaging,V40CloseArtifactReceipt,
    V40CloseArtifactHalted,_require_native,
)
from app.t100_v40_native_close_release_handoff import (
    V40NativeCloseReleaseHandoff,V40NativeCloseReleaseHandoffReport,
)
from app.t100_v40_portfolio_close_financial_reader import (
    V40NativeCloseFinancialEvidence,
)
from app.t100_v40_unreleased_verified_slot_release import (
    PostgresV40VerifiedSlotRelease,
)


class V40ForwardCloseHalted(RuntimeError):
    """Stable non-sensitive result; SQL/venue response bodies never logged."""


@dataclass(frozen=True)
class V40ForwardCloseResult:
    status: str
    position_id: str | None = None
    entry_and_close_fills_persisted: bool = False
    funding_persisted: bool = False
    final_accounting_persisted: bool = False
    slot_release_committed: bool = False
    historical_contract_multiplier_continuity_certified: bool = False
    post_release_new_account_get_required: bool = True
    entry_allowed: bool = False
    live_authorization: bool = False
    mexc_posts: int = 0
    database_commit_may_have_occurred: bool = False


class V40ForwardCloseFinalizer:
    """One closed position, one account, one fenced lease, zero exchange POST."""

    def __init__(
        self, *,staging:V40CloseOnlyFinancialArtifactStaging,
        final_accounting:PostgresFinalAccountingLedger,
        handoff:V40NativeCloseReleaseHandoff,
    )->None:
        if (
            type(staging) is not V40CloseOnlyFinancialArtifactStaging
            or type(final_accounting) is not PostgresFinalAccountingLedger
            or type(handoff) is not V40NativeCloseReleaseHandoff
            or staging.writer_pool is not final_accounting.pool
            or staging.writer_pool is staging.ledger.pool
            or handoff.writer.pool is not final_accounting.pool
            or handoff.probe is not staging.native.probe
            or staging.finance.probe is not handoff.probe
        ):
            raise V40ForwardCloseHalted(
                "V40_FORWARD_CLOSE_SAME_ACCOUNT_ROLE_AND_SIGNED_GET_REQUIRED"
            )
        self.staging=staging
        self.final_accounting=final_accounting
        self.handoff=handoff

    async def _verify_staged_hashes(
        self, *, finance:V40NativeCloseFinancialEvidence,
        lease:LeaderLease,
    )->None:
        """Rebind separately obtained signed GET proof to immutable custody."""
        if (
            type(finance) is not V40NativeCloseFinancialEvidence
            or type(lease) is not LeaderLease
        ):
            raise V40ForwardCloseHalted(
                "V40_FORWARD_CLOSE_FINANCE_OR_LEASE_MISSING"
            )
        pool=self.final_accounting.pool
        async with pool.acquire() as conn:
            async with conn.transaction(
                isolation="repeatable_read",readonly=True,
            ):
                if (
                    await conn.fetchval("SELECT current_user")!=TARGET_ROLE
                    or await conn.fetchval("SELECT current_database()")!=EXPECTED_DATABASE
                ):
                    raise V40ForwardCloseHalted(
                        "V40_FORWARD_CLOSE_RUNTIME_DB_ROLE_INVALID"
                    )
                lease_row=await conn.fetchrow(
                    """SELECT holder_id,fencing_token,
                              (lease_expires_at>clock_timestamp()) AS unexpired
                       FROM public.t100_live_executor_leases
                       WHERE lease_name='t100-live-executor'""",
                )
                if (
                    lease_row is None
                    or lease_row["holder_id"]!=lease.holder_id
                    or type(lease_row["fencing_token"]) is not int
                    or lease_row["fencing_token"]!=lease.fencing_token
                    or lease_row["unexpired"] is not True
                ):
                    raise V40ForwardCloseHalted(
                        "V40_FORWARD_CLOSE_STALE_LEADER_FENCE"
                    )
                for fills in (finance.entry_fills,finance.close_fills):
                    header=await conn.fetchrow(
                        """SELECT position_id,symbol,side,deal_volume,
                                  fill_count,fill_digest_sha256,
                                  strategy_fully_filled_order_complete
                           FROM public.t100_live_order_fill_sets
                           WHERE order_id=$1""",fills.order_id,
                    )
                    if (
                        header is None
                        or header["position_id"]!=fills.position_id
                        or header["symbol"]!=fills.symbol
                        or header["side"]!=fills.side
                        or D(str(header["deal_volume"]))!=fills.deal_volume
                        or header["fill_count"]!=len(fills.fills)
                        or header["fill_digest_sha256"]!=fills.fill_digest_sha256
                        or header["strategy_fully_filled_order_complete"] is not True
                    ):
                        raise V40ForwardCloseHalted(
                            "V40_FORWARD_CLOSE_PERSISTED_FILL_DIGEST_DRIFT"
                        )
                funding=await conn.fetchrow(
                    """SELECT total_count,funding_digest_sha256,
                              economics_verified,double_collection_finality_verified
                       FROM public.t100_live_position_funding_sets
                       WHERE position_id=$1""",finance.funding.position_id,
                )
                if (
                    funding is None
                    or funding["total_count"]!=finance.funding.total_count
                    or funding["funding_digest_sha256"]!=finance.funding.funding_digest_sha256
                    or funding["economics_verified"] is not True
                    or funding["double_collection_finality_verified"] is not True
                ):
                    raise V40ForwardCloseHalted(
                        "V40_FORWARD_CLOSE_PERSISTED_FUNDING_DIGEST_DRIFT"
                    )

    async def run_once(
        self,*,snapshot:FourSlotSnapshotResult,lease:LeaderLease,
    )->V40ForwardCloseResult:
        if (
            type(snapshot) is not FourSlotSnapshotResult
            or snapshot.status!="UNOWNED_OR_MISSING_CHECKPOINTS"
            or snapshot.entry_allowed is not False
            or snapshot.live_authorization is not False
            or type(lease) is not LeaderLease
            or lease.lease_name!="t100-live-executor"
            or type(lease.fencing_token) is not int
            or lease.fencing_token<=0
            or lease.live_authorization is not False
        ):
            raise V40ForwardCloseHalted(
                "V40_FORWARD_CLOSE_EXACT_NATIVE_MISMATCH_AND_FENCE_REQUIRED"
            )
        # Do not leak transaction ambiguity to a caller that might retry.
        try:
            staged=await self.staging.stage_once(snapshot=snapshot,lease=lease)
            if (
                type(staged) is not V40CloseArtifactReceipt
                or staged.status!=
                "V40_CLOSE_FILLS_AND_FUNDING_DURABLY_STAGED_FINAL_ACCOUNTING_NOT_CERTIFIED"
                or staged.immutable_entry_fills_verified is not True
                or staged.immutable_close_fills_verified is not True
                or staged.immutable_funding_verified is not True
                or staged.slot_released is not False
                or staged.live_authorization is not False
                or staged.mexc_posts!=0
                or staged.database_commit_may_have_occurred is not True
            ):
                raise V40ForwardCloseHalted("V40_FORWARD_CLOSE_STAGING_UNVERIFIED")

            # NEW exchange/SQL read, not a replayed mutable data object.
            native=await self.staging.native.observe(snapshot)
            if native.previous.position_id!=staged.position_id:
                raise V40ForwardCloseHalted(
                    "V40_FORWARD_CLOSE_DIFFERENT_POSITION_ON_REOBSERVATION"
                )
            finance=await self.staging.finance.observe(
                previous=native.previous,witness=native.witness,
            )
            _require_native(native,finance)
            await self._verify_staged_hashes(finance=finance,lease=lease)

            # Actual-deals and USDT funding economics are already calculated
            # by the typed financial reader. An archive/current multiplier
            # equality is required; unobserved intra-position history is NOT
            # labeled independently certified in any result.
            written=await self.final_accounting.persist(finance.accounting)
            if written not in ("PERSISTED","ALREADY_VERIFIED"):
                raise V40ForwardCloseHalted(
                    "V40_FORWARD_CLOSE_FINAL_ACCOUNTING_ACK_UNVERIFIED"
                )

            position=native.previous
            closing=OwnedShort(
                scope_sha256=self.staging.native.scope,symbol=position.symbol,
                position_id=position.position_id,
                entry_external_oid=position.external_oid,
                entry_order_id=position.exchange_order_id,
                contracts=position.contracts,
                stop_order_id=position.stop_order_id,
                stop_price=position.stop_price,
                checkpoint_version=position.checkpoint_version,
            )
            # This handoff makes another native signed GET and DB-CASes
            # only after verifying the FULL immutable final accounting row
            # under the account-wide lock and live lease fence.
            result=await self.handoff.run_once(
                closing=closing,previous=position,
                survivors=native.survivors,
                accounting=finance.accounting,lease=lease,
                account_scope_sha256=self.staging.native.scope,
            )
            if (
                type(result) is not V40NativeCloseReleaseHandoffReport
                or result.status!=
                "V40_CLOSE_HANDOFF_ONE_SLOT_DURABLY_RELEASED_NEW_GET_REQUIRED"
                or result.closed_position_id!=position.position_id
                or result.durable_slot_release_verified is not True
                or result.exact_exchange_close_verified is not True
                or result.persisted_accounting_verified is not True
                or result.fresh_account_ledger_guardian_recheck_required is not True
                or result.live_authorization is not False
                or result.next_entry_allowed is not False
                or result.mexc_posts!=0
            ):
                raise V40ForwardCloseHalted(
                    "V40_FORWARD_CLOSE_RELEASE_OUTCOME_UNKNOWN_RECONCILE"
                )
            return V40ForwardCloseResult(
                status="V40_FORWARD_NATIVE_CLOSE_DURABLY_ACCOUNTED_AND_SLOT_RELEASED_REOBSERVE",
                position_id=position.position_id,
                entry_and_close_fills_persisted=True,
                funding_persisted=True,final_accounting_persisted=True,
                slot_release_committed=True,
                database_commit_may_have_occurred=True,
            )
        except V40ForwardCloseHalted:
            raise
        except BaseException:
            # An opaque exception can hide an already committed DB
            # transaction. This is a hard stop: reconciliation, no retry.
            raise V40ForwardCloseHalted(
                "V40_FORWARD_CLOSE_OUTCOME_UNKNOWN_RECONCILE_ONLY"
            ) from None
