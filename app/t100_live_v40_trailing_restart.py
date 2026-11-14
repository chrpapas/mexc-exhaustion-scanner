"""Read-only V40 trailing restart attestation from durable checkpoint history.

This module does NOT infer an intrabar low or write to PostgreSQL.
It validates the entire immutable checkpoint revision sequence before
resuming frozen 14%-arm/0.5pp-gap trailing calculations. The recovered
checkpoint best/armed flags are a persistent FLOOR, not rewritten from
the last observed candle. Candidate advances require a certified
post-entry low stream; completed-candle history alone is incomplete
when the fill occurred part-way through the first 30m candle.

No state mutations, stop POST, live permit, or silent reset is possible.
"""
from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal as D
from typing import Any

from app.t100_live_durable_recovery import (
    DurableRecoveryHalted, PositionCheckpoint,
    _validate_revision_history, checkpoint_from_row,
)
from app.t100_live_trailing import next_short_stop, TrailStep


class V40TrailingRestartHalted(RuntimeError):
    pass


@dataclass(frozen=True)
class V40TrailingRestartView:
    status: str
    position_id: str
    symbol: str
    checkpoint_version: int
    recovered_best_profit_pct: D
    recovered_armed: bool
    recovered_stop_price: D
    next_best_profit_pct: D
    next_armed: bool
    proposed_stop_price: D
    proposed_stop_tightening: bool
    durable_revision_history_verified: bool
    post_fill_low_stream_complete: bool = False
    stop_amendment_authorized: bool = False
    live_authorization: bool = False


def reconstruct_v40_trailing_after_restart(
    *,
    checkpoint_row: dict[str,Any],
    revision_rows: list[dict[str,Any]],
    observed_post_entry_low: D | None,
    price_tick: D,
    signed_fill_time_verified: bool = False,
    full_post_fill_low_stream_verified: bool = False,
) -> V40TrailingRestartView:
    """History + monotonic trailing state; never claims historical tick coverage.

    Even if an independent live tick stream is later supplied, this module
    does not itself establish the reliability of that stream. It only
    calculates the frozen next decision from the durable checkpoint.
    """
    if (
        type(checkpoint_row) is not dict
        or type(revision_rows) is not list
        or type(price_tick) is not D
        or not price_tick.is_finite() or price_tick<=0
        or type(signed_fill_time_verified) is not bool
        or type(full_post_fill_low_stream_verified) is not bool
    ):
        raise V40TrailingRestartHalted("TRAILING_RESTART_INPUT_INVALID")

    try:
        cp=checkpoint_from_row(checkpoint_row)
        _validate_revision_history((cp,),revision_rows)
    except (DurableRecoveryHalted,ValueError,TypeError):
        raise V40TrailingRestartHalted(
            "TRAILING_RESTART_DURABLE_HISTORY_UNVERIFIED"
        ) from None

    if observed_post_entry_low is not None and (
        type(observed_post_entry_low) is not D
        or not observed_post_entry_low.is_finite()
        or observed_post_entry_low<=0
    ):
        raise V40TrailingRestartHalted("TRAILING_RESTART_LOW_INVALID")
    if full_post_fill_low_stream_verified and not signed_fill_time_verified:
        raise V40TrailingRestartHalted(
            "TRAILING_RESTART_LOW_WITHOUT_SIGNED_FILL_ORIGIN"
        )

    if observed_post_entry_low is None:
        step=TrailStep(
            active=cp.armed,best_profit_pct=cp.best_profit_pct,
            stop_price=cp.stop_price,changed=False,
        )
    else:
        try:
            step=next_short_stop(
                entry=cp.entry_price,
                low=observed_post_entry_low,
                current_stop=cp.stop_price,
                best_profit_pct=cp.best_profit_pct,
                price_tick=price_tick,
                armed=cp.armed,
            )
        except (ArithmeticError,ValueError):
            raise V40TrailingRestartHalted("TRAILING_RESTART_CALCULATION_INVALID") from None

    # A partial history cannot justify a new trailing amendment; retain
    # the persistent best/armed floor and do not downgrade existing stops.
    verified_low=(
        signed_fill_time_verified and full_post_fill_low_stream_verified
    )
    proposed=step.stop_price if verified_low else cp.stop_price
    tightening=step.changed if verified_low else False
    return V40TrailingRestartView(
        status=(
            "V40_TRAILING_RESTART_CANDIDATE_REBUILT_NONAUTHORIZING"
            if verified_low else
            "V40_TRAILING_RESTART_DURABLE_FLOOR_ONLY_FREEZE"
        ),
        position_id=cp.position_id,symbol=cp.symbol,
        checkpoint_version=cp.checkpoint_version,
        recovered_best_profit_pct=cp.best_profit_pct,
        recovered_armed=cp.armed,recovered_stop_price=cp.stop_price,
        next_best_profit_pct=max(cp.best_profit_pct,step.best_profit_pct)
            if verified_low else cp.best_profit_pct,
        next_armed=cp.armed or (step.active if verified_low else False),
        proposed_stop_price=proposed,
        proposed_stop_tightening=tightening,
        durable_revision_history_verified=True,
        post_fill_low_stream_complete=verified_low,
    )
