"""Read-only bridge from completed post-fill Min30 lows to V40 stop preflight.

This module integrates the causal PostgreSQL low reader with existing strict
1-4 position signed-GET/native-stop snapshot checks. The resulting proposal
is OBSERVATION ONLY and is *not* a journal/POST permit.

The first partly elapsed 30-minute entry candle cannot contribute a
favorable low because it might predate the signed fill. This conservative
history starts with the next fully completed bar. Its missing first-bar
coverage is explicitly kept false; a complete venue-timestamped tick/price
history and independently signed fill evidence are still required before
any real stop modification may be certified.

No SQL writes, no MEXC API, no stop mutations and no key access.
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal as D
from typing import Protocol, Sequence

from app.t100_live_durable_recovery import PositionCheckpoint
from app.t100_live_trailing_preflight import (
    TrailingBookend, review_t100_trailing_candidate,
)
from app.t100_live_v40_trailing_low_source import V40CausalLow
from app.t100_v40_admission import BAR_SECONDS


class V40TrailingReviewHalted(RuntimeError):
    pass


class CausalTrailingReader(Protocol):
    async def read(
        self, *, symbol: str, filled_at: datetime, observed_at: datetime,
    ) -> V40CausalLow: ...


@dataclass(frozen=True)
class V40CausalTrailingReview:
    status: str
    symbol: str
    position_id: str
    completed_causal_bars: int = 0
    observed_minimum_price: D | None = None
    proposed_stop_price: D | None = None
    last_completed_bar_open_ts: int | None = None
    exchange_signed_fill_time_verified: bool = False
    full_post_fill_minimum_coverage_verified: bool = False
    independently_verified_market_low: bool = False
    exchange_stop_mutation_allowed: bool = False
    entry_allowed: bool = False
    live_authorization: bool = False


async def review_v40_trailing_from_completed_candles(
    *,
    reader: CausalTrailingReader,
    checkpoints: list[PositionCheckpoint],
    target_position_id: str,
    first: TrailingBookend,
    second: TrailingBookend,
    filled_at: datetime,
    observed_at: datetime,
    current_last_price: D,
    price_tick: D,
    market_observed_at_ms: int,
    now_ms: int,
) -> V40CausalTrailingReview:
    if (
        not hasattr(reader,"read")
        or type(checkpoints) is not list
        or type(target_position_id) is not str
        or type(filled_at) is not datetime
        or type(observed_at) is not datetime
        or filled_at.tzinfo is None or observed_at.tzinfo is None
        or type(now_ms) is not int or now_ms <= 0
    ):
        raise V40TrailingReviewHalted("V40_TRAILING_PROVENANCE_INPUT_INVALID")
    matched=[cp for cp in checkpoints
             if type(cp) is PositionCheckpoint
             and cp.position_id==target_position_id]
    if len(matched)!=1 or matched[0].lifecycle_state!="OPEN":
        raise V40TrailingReviewHalted("V40_TRAILING_CHECKPOINT_ORIGIN_INVALID")
    cp=matched[0]
    fill_ms=filled_at.timestamp()*1000
    obs_ms=observed_at.timestamp()*1000
    if (
        not math.isfinite(fill_ms) or not math.isfinite(obs_ms)
        or fill_ms>=obs_ms
        or obs_ms>now_ms+1000
        or now_ms-obs_ms>10000
    ):
        raise V40TrailingReviewHalted("V40_TRAILING_CLOCK_OR_CUTOFF_INVALID")
    try:
        causal=await reader.read(
            symbol=cp.symbol,filled_at=filled_at,
            observed_at=observed_at,
        )
    except Exception:
        # Never expose DSN, symbol/account identifiers, SQL, or raw errors.
        raise V40TrailingReviewHalted(
            "V40_TRAILING_CAUSAL_HISTORY_READ_FAILED"
        ) from None
    if type(causal) is not V40CausalLow or causal.symbol!=cp.symbol:
        raise V40TrailingReviewHalted("V40_TRAILING_CAUSAL_SOURCE_INVALID")
    if causal.status=="V40_NO_CAUSAL_COMPLETED_BARS_NONAUTHORIZING":
        if causal.observed_low is not None or causal.complete_bars!=0:
            raise V40TrailingReviewHalted("V40_TRAILING_CAUSAL_EMPTY_CONTRADICTION")
        return V40CausalTrailingReview(
            "V40_TRAILING_NO_COMPLETE_POST_FILL_CANDLES_NONAUTHORIZING",
            cp.symbol,cp.position_id,
        )
    if (
        causal.status!="V40_COMPLETED_MIN30_CAUSAL_LOW_NONAUTHORIZING"
        or type(causal.complete_bars) is not int or causal.complete_bars<1
        or type(causal.observed_low) is not D
        or not causal.observed_low.is_finite()
        or causal.observed_low<=0
        or causal.complete_bars>12000
        or causal.covers_since_entry_fill is not False
        or causal.signed_fill_provenance_verified is not False
        or causal.stop_amendment_authorized is not False
    ):
        raise V40TrailingReviewHalted("V40_TRAILING_CAUSAL_EVIDENCE_INVALID")
    first_expected=(
        math.ceil(filled_at.timestamp()/BAR_SECONDS)*BAR_SECONDS
    )
    last_expected=(
        math.floor(observed_at.timestamp()/BAR_SECONDS)*BAR_SECONDS
        -BAR_SECONDS
    )
    if (
        causal.first_completed_open_ts!=first_expected
        or causal.last_completed_open_ts!=last_expected
        or causal.complete_bars!=(
            (last_expected-first_expected)//BAR_SECONDS+1
        )
    ):
        raise V40TrailingReviewHalted("V40_TRAILING_CAUSAL_WINDOW_DRIFT")

    review=review_t100_trailing_candidate(
        checkpoints=checkpoints,target_position_id=target_position_id,
        first=first,second=second,
        observed_low_since_entry=causal.observed_low,
        current_last_price=current_last_price,price_tick=price_tick,
        market_observed_at_ms=market_observed_at_ms,
        now_ms=now_ms,
    )
    if review.status=="TRAILING_PROPOSAL_OBSERVED_NONAUTHORIZING":
        if review.proposal is None:
            raise V40TrailingReviewHalted("V40_TRAILING_EMPTY_PROPOSAL")
        return V40CausalTrailingReview(
            status="V40_TRAILING_COMPLETED_CANDLE_PROPOSAL_ONLY_NOT_AMENDABLE",
            symbol=cp.symbol,position_id=cp.position_id,
            completed_causal_bars=causal.complete_bars,
            observed_minimum_price=causal.observed_low,
            proposed_stop_price=review.proposal.proposed_stop,
            last_completed_bar_open_ts=causal.last_completed_open_ts,
        )
    # Even when preflight finds no change or mismatched full-volume stop,
    # NEVER escalate this result to an executable stop mutation.
    return V40CausalTrailingReview(
        status="V40_TRAILING_PREFLIGHT_BLOCKED_"+review.status,
        symbol=cp.symbol,position_id=cp.position_id,
        completed_causal_bars=causal.complete_bars,
        observed_minimum_price=causal.observed_low,
        last_completed_bar_open_ts=causal.last_completed_open_ts,
    )
