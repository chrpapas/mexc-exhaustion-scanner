"""Unreleased V40 owned 1–4-slot independently due completed-bar handoff from durable facts.

Ties together independently read-only account/stop bookends, immutable
ledger checkpoints and durable Min30 bar cursors before using the frozen
per-slot SL75/14%-arm/0.5pp-gap decision batch. This is an observation/
projection only: no exchange POST, SQL write, new slot or release permit.

A synthesized policy checkpoint without its exact recovered exchange
position and immutable pending-free cursor MUST NOT be trusted.
"""
from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal as D
from typing import Mapping,Sequence

from app.t100_live_four_slot_snapshot_coordinator import (
    FourSlotSnapshotResult,LedgerSnapshot,
)
from app.t100_live_durable_recovery import (
    checkpoint_from_row,DurableRecoveryHalted,
)
from app.t100_v40_four_slot_guardian_policy import (
    audit_frozen_four_slot_native_stops,
)
from app.t100_v40_unreleased_bar_cursor import V40BarCursorState
from app.t100_v40_unreleased_exit_policy import V40CompletedBar,V40ExitCheckpoint
from app.t100_v40_unreleased_portfolio_exit_batch import (
    V40PortfolioExitBatch,project_v40_portfolio_exit_batch,
    V40PortfolioExitBatchHalted,
)


class V40VerifiedExitHalted(RuntimeError):
    """Fixed-category no-secret failure, no partial batch or side effect."""


@dataclass(frozen=True)
class V40VerifiedExitProjection:
    status: str
    protected_position_count: int
    verified_exchange_and_ledger_bookends: bool
    verified_durable_bar_cursors: bool
    batch: V40PortfolioExitBatch
    native_close_reconciliation_count: int
    candidate_native_stop_amendment_count: int
    live_execution_certified: bool = False
    entries_permitted: bool = False
    stop_amendment_permitted: bool = False
    position_release_permitted: bool = False
    exchange_posts: int = 0
    database_writes: int = 0


def project_verified_owned_v40_exits(
    *,
    snapshot: FourSlotSnapshotResult,
    ledger: LedgerSnapshot,
    signed_account_positions: list[dict],
    signed_native_stops: list[dict],
    cursors: Sequence[V40BarCursorState],
    completed_bars_by_symbol: Mapping[str,V40CompletedBar],
    price_ticks_by_symbol: Mapping[str,D],
    now_ts: int,
) -> V40VerifiedExitProjection:
    """Requires *caller-supplied* fresh signed GET and read-only SQL bookends.

    Caller must obtain snapshot and matching venue rows from the very same
    FourSlotSnapshotCoordinator cycle, then read immutable ledger/cursor data
    in a controlled window. It must independently recheck freshness before
    any subsequent mutation; this function never approves mutations.
    """
    if (
        type(snapshot) is not FourSlotSnapshotResult
        or type(ledger) is not LedgerSnapshot
        or type(cursors) not in (list,tuple)
        or not 1<=len(cursors)<=4
        or any(type(x) is not V40BarCursorState for x in cursors)
        or type(now_ts) is not int
        or type(completed_bars_by_symbol) is not dict
        or type(price_ticks_by_symbol) is not dict
    ):
        raise V40VerifiedExitHalted("V40_VERIFIED_EXIT_INPUT_INVALID")
    protection=audit_frozen_four_slot_native_stops(
        snapshot=snapshot,ledger=ledger,
        account_positions=signed_account_positions,
        account_stops=signed_native_stops,
    )
    if not (
        protection.status=="V40_FOUR_SLOT_ACCOUNT_NATIVE_MARKET_SL_SIGNED_GET_NONAUTHORIZING"
        and protection.protected_owned_positions==len(cursors)
        and protection.matching_native_market_stops==len(cursors)
        and protection.separate_symbols_verified is True
        and protection.account_bookends_verified is True
        and protection.ledger_bookends_verified is True
        and protection.all_owned_cross_positions_verified is True
        and protection.incident_alert is False
        and protection.live_authorization is False
        and protection.mexc_posts==protection.database_writes==0
    ):
        raise V40VerifiedExitHalted("V40_VERIFIED_EXIT_ACCOUNT_OR_STOP_UNVERIFIED")

    try:
        checkpoints=tuple(checkpoint_from_row(row) for row in ledger.checkpoints)
    except (DurableRecoveryHalted,TypeError,ValueError):
        raise V40VerifiedExitHalted("V40_VERIFIED_EXIT_CHECKPOINT_INVALID") from None
    if (
        len(checkpoints)!=len(cursors)
        or len({c.position_id for c in cursors})!=len(cursors)
        or {cp.position_id for cp in checkpoints}!={c.position_id for c in cursors}
    ):
        raise V40VerifiedExitHalted("V40_VERIFIED_EXIT_CURSOR_OWNERSHIP_DRIFT")
    cursor_by_id={c.position_id:c for c in cursors}
    policy_checkpoints=[]
    for cp in checkpoints:
        c=cursor_by_id[cp.position_id]
        if (
            cp.lifecycle_state!="OPEN"
            or c.entry_external_oid!=cp.external_oid
            or c.stop_order_id!=cp.stop_order_id
            or c.confirmed_stop_price!=cp.stop_price
            or c.best_profit_pct!=cp.best_profit_pct
            or c.trail_armed is not cp.armed
            or c.checkpoint_version!=cp.checkpoint_version
            or type(c.cursor_revision) is not int or c.cursor_revision<=0
            or type(c.signed_fill_at_ms) is not int or c.signed_fill_at_ms<=0
            or c.pending_open_ts is not None
            or c.pending_status is not None
            or c.pending_stop_price is not None
            or c.pending_best_profit_pct is not None
            or c.pending_trail_armed is not None
        ):
            raise V40VerifiedExitHalted("V40_VERIFIED_EXIT_PENDING_OR_CURSOR_DRIFT")
        # Millisecond ceil: exclude any bar that began before the actual
        # verified entry fill. Never infer a pre-position intrabar low.
        policy_checkpoints.append(V40ExitCheckpoint(
            position_id=cp.position_id,symbol=cp.symbol,
            entry_price=cp.entry_price,
            filled_at_ts=(c.signed_fill_at_ms+999)//1000,
            native_stop_price=cp.stop_price,
            best_profit_pct=cp.best_profit_pct,
            trail_armed=cp.armed,
            last_completed_open_ts=c.last_committed_open_ts,
        ))
    # Cursor phase is independent per position: even during the same
    # wall-clock Min30 interval, some positions can have no due bar.
    # We audited ALL open positions and their exact native full-volume SLs
    # above. Only due positions enter the no-POST decision batch.
    all_symbols={x.symbol for x in policy_checkpoints}
    due_symbols=set(completed_bars_by_symbol)
    if (
        not due_symbols
        or not due_symbols.issubset(all_symbols)
        or due_symbols!=set(price_ticks_by_symbol)
    ):
        raise V40VerifiedExitHalted("V40_VERIFIED_EXIT_DUE_BAR_SET_INVALID")
    due_checkpoints=tuple(x for x in policy_checkpoints if x.symbol in due_symbols)
    try:
        batch=project_v40_portfolio_exit_batch(
            checkpoints=due_checkpoints,
            completed_bars_by_symbol=completed_bars_by_symbol,
            price_ticks_by_symbol=price_ticks_by_symbol,
            now_ts=now_ts,
        )
    except (V40PortfolioExitBatchHalted,ValueError,TypeError,ArithmeticError):
        raise V40VerifiedExitHalted("V40_VERIFIED_EXIT_COMPLETED_BAR_OR_STOP_HALT") from None
    if (
        batch.live_authorization is not False
        or batch.mexc_posts!=0 or batch.database_writes!=0
        or batch.can_release_position_slot is not False
        or batch.can_submit_exchange_stop_amendment is not False
        or batch.can_admit_new_position is not False
    ):
        raise V40VerifiedExitHalted("V40_VERIFIED_EXIT_MODEL_CONTRACT_DRIFT")
    return V40VerifiedExitProjection(
        status="V40_VERIFIED_OWNED_ONE_TO_FOUR_EXITS_PROJECTED_NONAUTHORIZING",
        protected_position_count=len(checkpoints),
        verified_exchange_and_ledger_bookends=True,
        verified_durable_bar_cursors=True,
        batch=batch,
        native_close_reconciliation_count=batch.native_close_reconciliation_count,
        candidate_native_stop_amendment_count=batch.proposed_stop_amendment_count,
    )
