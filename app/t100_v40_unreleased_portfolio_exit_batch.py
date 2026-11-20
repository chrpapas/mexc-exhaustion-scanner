"""UNRELEASED, pure four-slot V40 completed-bar exit orchestration.

This is the missing read-only composition of the existing frozen SL75/A14/G0.5
single-position exit policy across 1–4 independently owned SHORT slots.

Every slot must have exactly one complete consecutive Min30 candle, one tick
specification, and a unique exchange position ID/symbol. The batch commits
nothing: a touched stop is NOT a proven fill; a proposed new stop is NOT
active until separately verified on MEXC and durably checkpointed.

A single malformed/stale/missing frame rejects the entire batch. The
projector deliberately cannot amend a stop, release a slot, or issue a trade.
"""
from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal as D
from typing import Mapping, Sequence

from app.t100_v40_unreleased_exit_policy import (
    V40CompletedBar,
    V40ExitCheckpoint,
    V40ExitDecision,
    V40ExitPolicyHalted,
    propose_next_completed_bar,
)

MAX_V40_SLOTS = 4

class V40PortfolioExitBatchHalted(RuntimeError):
    """A fixed nonsecret fail-closed reason."""


@dataclass(frozen=True)
class V40SlotExitReview:
    position_id: str
    symbol: str
    decision: V40ExitDecision
    requires_native_order_reconciliation: bool
    requires_native_stop_amendment: bool
    proposed_stop_price: D | None
    # These remain false until a completely different, signed GET+SQL
    # coordinator has verified actual venue outcomes.
    position_closed_verified: bool = False
    stop_mutation_verified: bool = False
    durable_checkpoint_committed: bool = False


@dataclass(frozen=True)
class V40PortfolioExitBatch:
    status: str
    slots: tuple[V40SlotExitReview, ...]
    native_close_reconciliation_count: int
    proposed_stop_amendment_count: int
    unchanged_verified_stop_count: int
    any_unresolved_venue_transition: bool
    can_release_position_slot: bool = False
    can_submit_exchange_stop_amendment: bool = False
    can_admit_new_position: bool = False
    historical_execution_parity_certified: bool = False
    live_authorization: bool = False
    mexc_posts: int = 0
    database_writes: int = 0


def project_v40_portfolio_exit_batch(
    *,
    checkpoints: Sequence[V40ExitCheckpoint],
    completed_bars_by_symbol: Mapping[str,V40CompletedBar],
    price_ticks_by_symbol: Mapping[str,D],
    now_ts: int,
) -> V40PortfolioExitBatch:
    """All-or-nothing read-only observation of exactly one bar per owned slot.

    No member of a return value should be passed to an exchange POST without
    an independent owner/fill/stop readback, durable journal, CAS and fresh
    account-wide inventory. A triggered native stop must be correlated with
    its original stop ID and positive closing fills; candle touches alone
    never certify a close.
    """
    if (
        type(checkpoints) not in (tuple,list)
        or not 1 <= len(checkpoints) <= MAX_V40_SLOTS
        or type(completed_bars_by_symbol) is not dict
        or type(price_ticks_by_symbol) is not dict
        or type(now_ts) is not int
        or now_ts < 0
    ):
        raise V40PortfolioExitBatchHalted("V40_EXIT_BATCH_INPUT_INVALID")
    # Check shape before property access; also reject duplicate positions
    # even if the two identities have distinct symbols.
    if any(type(x) is not V40ExitCheckpoint for x in checkpoints):
        raise V40PortfolioExitBatchHalted("V40_EXIT_BATCH_CHECKPOINT_INVALID")
    ids=[x.position_id for x in checkpoints]
    symbols=[x.symbol for x in checkpoints]
    if (
        len(set(ids))!=len(ids)
        or len(set(symbols))!=len(symbols)
        or set(symbols)!=set(completed_bars_by_symbol)
        or set(symbols)!=set(price_ticks_by_symbol)
        or any(type(k) is not str for k in completed_bars_by_symbol)
    ):
        raise V40PortfolioExitBatchHalted("V40_EXIT_BATCH_IDENTITY_OR_BAR_COVERAGE_INVALID")

    reviews: list[V40SlotExitReview]=[]
    for cp in checkpoints:
        bar=completed_bars_by_symbol[cp.symbol]
        tick=price_ticks_by_symbol[cp.symbol]
        if type(bar) is not V40CompletedBar or type(tick) is not D:
            raise V40PortfolioExitBatchHalted("V40_EXIT_BATCH_BAR_OR_TICK_INVALID")
        try:
            decision=propose_next_completed_bar(
                cp,bar,price_tick=tick,now_ts=now_ts,
            )
        except (V40ExitPolicyHalted,ValueError,ArithmeticError):
            # Entire portfolio batch is invalid. No partial result/action.
            raise V40PortfolioExitBatchHalted("V40_EXIT_BATCH_SLOT_STATE_UNVERIFIED") from None
        if type(decision) is not V40ExitDecision:
            raise V40PortfolioExitBatchHalted("V40_EXIT_BATCH_DECISION_INVALID")
        closing=decision.status=="NATIVE_STOP_MAY_HAVE_TRIGGERED_RECONCILE_ONLY"
        amending=decision.status=="NATIVE_STOP_TIGHTENING_PROPOSED_UNAUTHORIZED"
        keeping=decision.status=="KEEP_VERIFIED_NATIVE_STOP"
        if (
            not (closing or amending or keeping)
            or decision.exchange_orders_sent!=0
            or decision.live_authorization is not False
            or decision.requires_venue_exit_reconciliation is not closing
            or decision.requires_exchange_stop_amendment is not amending
            or decision.may_persist_without_verified_exchange_readback is not False
            or (closing and decision.proposed_checkpoint is not None)
            or (amending and decision.proposed_native_stop_price is None)
            or (keeping and decision.proposed_native_stop_price is not None)
        ):
            raise V40PortfolioExitBatchHalted("V40_EXIT_BATCH_DECISION_CONTRACT_DRIFT")
        reviews.append(V40SlotExitReview(
            position_id=cp.position_id,symbol=cp.symbol,decision=decision,
            requires_native_order_reconciliation=closing,
            requires_native_stop_amendment=amending,
            proposed_stop_price=decision.proposed_native_stop_price,
        ))

    exits=sum(s.requires_native_order_reconciliation for s in reviews)
    amendments=sum(s.requires_native_stop_amendment for s in reviews)
    kept=len(reviews)-exits-amendments
    if exits:
        status="V40_PORTFOLIO_NATIVE_EXIT_UNVERIFIED_RECONCILE_ONLY"
    elif amendments:
        status="V40_PORTFOLIO_NATIVE_STOP_AMENDMENTS_UNVERIFIED"
    else:
        status="V40_PORTFOLIO_VERIFIED_STOP_MODEL_UNCHANGED_NONAUTHORIZING"
    return V40PortfolioExitBatch(
        status=status,slots=tuple(reviews),
        native_close_reconciliation_count=exits,
        proposed_stop_amendment_count=amendments,
        unchanged_verified_stop_count=kept,
        any_unresolved_venue_transition=(exits+amendments>0),
    )
