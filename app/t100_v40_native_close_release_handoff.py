"""One-shot positive native close -> durable single-slot release orchestration.

UNRELEASED. No worker, credentials, exchange POST, stop amendment, order
retry, or permission to open the next position. An actual signed MEXC
native-stop close witness is a prerequisite, as is independently reconciled
entry+close+funding accounting already persisted in PostgreSQL. The existing
PostgresV40VerifiedSlotRelease writer enforces immutable SQL identity,
exclusive advisory lock, leader fencing and one-transaction CAS.

Any uncertain exchange observation => zero DB writes.
Any uncertain DB commit => UNKNOWN, no second attempt or new entry.
A confirmed DB release STILL requires a brand-new account/ledger/guardian
observation before any next entry.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime,timezone
from typing import Any, Sequence

from app.t100_live_durable_recovery import PositionCheckpoint
from app.t100_live_leader_fencing import LeaderLease
from app.t100_live_owned_emergency_guardian import OwnedShort
from app.t100_live_position_accounting import VerifiedClosedPositionAccounting
from app.t100_live_readonly_probe import ReadOnlyMexcProbe
from app.t100_v40_unreleased_portfolio_native_close import (
    V40PortfolioCloseWitness, observe_one_native_exit_among_v40_survivors,
)
from app.t100_v40_unreleased_verified_slot_release import (
    PostgresV40VerifiedSlotRelease,V40SlotReleaseResult,
    V40SlotReleaseHalted,_proofs_match,
)


@dataclass(frozen=True)
class V40NativeCloseReleaseHandoffReport:
    status: str
    closed_position_id: str | None = None
    exact_exchange_close_verified: bool = False
    persisted_accounting_verified: bool = False
    durable_slot_release_verified: bool = False
    database_commit_may_have_occurred: bool = False
    fresh_account_ledger_guardian_recheck_required: bool = True
    next_entry_allowed: bool = False
    live_authorization: bool = False
    mexc_posts: int = 0


class V40NativeCloseReleaseHandoff:
    """Do not wire into Render until the real positive-fill/accounting path exists."""

    def __init__(
        self, *, probe: ReadOnlyMexcProbe,
        writer: PostgresV40VerifiedSlotRelease,
    ) -> None:
        if (
            type(probe) is not ReadOnlyMexcProbe
            or type(writer) is not PostgresV40VerifiedSlotRelease
        ):
            raise TypeError("V40_NATIVE_CLOSE_HANDOFF_DEPENDENCY_INVALID")
        self.probe=probe
        self.writer=writer

    async def run_once(
        self, *, closing: OwnedShort, previous: PositionCheckpoint,
        survivors: Sequence[PositionCheckpoint],
        accounting: VerifiedClosedPositionAccounting,
        lease: LeaderLease,
        account_scope_sha256: str,
    ) -> V40NativeCloseReleaseHandoffReport:
        halt=lambda reason:V40NativeCloseReleaseHandoffReport(status=reason)
        if (
            type(closing) is not OwnedShort
            or type(previous) is not PositionCheckpoint
            or type(survivors) not in (tuple,list)
            or len(survivors)>3
            or any(type(p) is not PositionCheckpoint for p in survivors)
            or type(accounting) is not VerifiedClosedPositionAccounting
            or type(lease) is not LeaderLease
            or type(account_scope_sha256) is not str
            or len(account_scope_sha256)!=64
            or not account_scope_sha256.isascii()
            or any(c not in "0123456789abcdef" for c in account_scope_sha256)
        ):
            return halt("V40_CLOSE_HANDOFF_PROVENANCE_INPUT_INVALID")
        if (
            closing.scope_sha256!=account_scope_sha256
            or previous.position_id!=closing.position_id
            or previous.symbol!=closing.symbol
            or previous.external_oid!=closing.entry_external_oid
            or previous.exchange_order_id!=closing.entry_order_id
            or previous.contracts!=closing.contracts
            or previous.stop_order_id!=closing.stop_order_id
            or previous.stop_price!=closing.stop_price
            or previous.checkpoint_version!=closing.checkpoint_version
            or previous.lifecycle_state!="OPEN"
            or closing.checkpoint_lifecycle!="OPEN"
            or len({p.position_id for p in survivors})!=len(survivors)
            or previous.position_id in {p.position_id for p in survivors}
            or previous.symbol in {p.symbol for p in survivors}
        ):
            return halt("V40_CLOSE_HANDOFF_OWNED_POSITION_MISMATCH")
        try:
            witness=await observe_one_native_exit_among_v40_survivors(
                probe=self.probe,closing=closing,
                account_scope_sha256=account_scope_sha256,
                surviving=survivors,
            )
        except Exception:
            return halt("V40_CLOSE_HANDOFF_SIGNED_GET_UNVERIFIED")
        if (
            type(witness) is not V40PortfolioCloseWitness
            or witness.status not in (
                "V40_PORTFOLIO_CLOSE_EXCHANGE_TP_LABEL_CONFLICT_UNRELEASED",
                "V40_PORTFOLIO_CLOSE_EXCHANGE_SL_LABEL_OBSERVED_UNRELEASED",
            )
            or witness.closed_position_id!=previous.position_id
            or witness.surviving_protected_slots!=len(survivors)
        ):
            return halt("V40_CLOSE_HANDOFF_NATIVE_TERMINAL_NOT_CERTIFIED")
        # Current witness time is taken only *after* the last authenticated
        # GET bookend; the SQL writer independently checks it is <=2s old.
        observed_at=datetime.now(timezone.utc)
        try:
            _proofs_match(
                witness=witness,previous=previous,accounting=accounting,
                lease=lease,observed_at=observed_at,
            )
        except V40SlotReleaseHalted:
            return halt("V40_CLOSE_HANDOFF_ACCOUNTING_OR_FILL_PROVENANCE_INVALID")
        try:
            result=await self.writer.close_and_release_once(
                witness=witness,previous=previous,accounting=accounting,
                lease=lease,observed_at=observed_at,
            )
        except BaseException:
            # A transaction may already have committed; do not retry.
            return V40NativeCloseReleaseHandoffReport(
                "V40_CLOSE_HANDOFF_SQL_OUTCOME_UNKNOWN_RECONCILE_ONLY",
                closed_position_id=previous.position_id,
                exact_exchange_close_verified=True,
                persisted_accounting_verified=True,
                database_commit_may_have_occurred=True,
            )
        if (
            type(result) is not V40SlotReleaseResult
            or result.status!="V40_CLOSE_DB_RELEASE_COMMITTED_RESTART_GET_REQUIRED"
            or result.position_id!=previous.position_id
            or result.durable_release_and_close_committed is not True
            or result.post_commit_signed_get_required is not True
            or result.next_entry_allowed is not False
            or result.live_authorization is not False
            or result.mexc_posts!=0
        ):
            return V40NativeCloseReleaseHandoffReport(
                "V40_CLOSE_HANDOFF_RELEASE_ACK_UNVERIFIED_RECONCILE_ONLY",
                closed_position_id=previous.position_id,
                exact_exchange_close_verified=True,
                persisted_accounting_verified=True,
                database_commit_may_have_occurred=True,
            )
        return V40NativeCloseReleaseHandoffReport(
            "V40_CLOSE_HANDOFF_ONE_SLOT_DURABLY_RELEASED_NEW_GET_REQUIRED",
            closed_position_id=previous.position_id,
            exact_exchange_close_verified=True,
            persisted_accounting_verified=True,
            durable_slot_release_verified=True,
            database_commit_may_have_occurred=True,
        )
