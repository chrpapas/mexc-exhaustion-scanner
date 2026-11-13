"""Frozen V40 SHORT exit decisions for a future live stop manager.

UNRELEASED. Pure computation; NEVER POST, write to a DB, infer an actual exit
from a candle, or authorize a native-stop amendment.

One strictly complete, consecutive 30-minute candle is processed at a time.
The previous exchange-verified native stop is checked against its adverse HIGH
before any favorable LOW can arm/tighten the trailing stop. Proposed stop
changes only become durable after an independent MEXC stop readback and CAS
checkpoint commit performed by an external coordinator.

The model is the original SL75 / A14 / G0.5 strategy, NOT the older TP5.
"""
from __future__ import annotations

from dataclasses import dataclass, replace
from decimal import Decimal as D, ROUND_UP
from typing import Literal

from app.t100_200_strategy import STOP_PCT, TRAIL_ACTIVATION_PCT, TRAIL_GAP_PCT

BAR_SECONDS = 1800
STATUS = Literal[
    "KEEP_VERIFIED_NATIVE_STOP",
    "NATIVE_STOP_TIGHTENING_PROPOSED_UNAUTHORIZED",
    "NATIVE_STOP_MAY_HAVE_TRIGGERED_RECONCILE_ONLY",
    "FROZEN_V40_BAR_OR_STOP_UNVERIFIED_HALT",
]


class V40ExitPolicyHalted(ValueError):
    """A fixed, nonsecret status. Never include MEXC/SQL response bodies."""


def _positive(value: D) -> bool:
    return type(value) is D and value.is_finite() and value > 0


def _nonnegative(value: D) -> bool:
    return type(value) is D and value.is_finite() and value >= 0


@dataclass(frozen=True)
class V40CompletedBar:
    open_ts: int
    open: D
    high: D
    low: D
    close: D

    def validate(self, now_ts: int) -> None:
        if (
            type(self.open_ts) is not int
            or type(now_ts) is not int
            or self.open_ts < 0
            or self.open_ts % BAR_SECONDS != 0
            or self.open_ts + BAR_SECONDS > now_ts
            or any(not _positive(x) for x in
                   (self.open, self.high, self.low, self.close))
            or self.high < max(self.open, self.close, self.low)
            or self.low > min(self.open, self.close, self.high)
        ):
            raise V40ExitPolicyHalted("FROZEN_V40_BAR_OR_STOP_UNVERIFIED_HALT")


@dataclass(frozen=True)
class V40ExitCheckpoint:
    """Only construct from a verified, durable exchange position/checkpoint."""

    position_id: str
    symbol: str
    entry_price: D
    filled_at_ts: int
    native_stop_price: D
    best_profit_pct: D = D("0")
    trail_armed: bool = False
    last_completed_open_ts: int | None = None

    def validate(self) -> None:
        if (
            type(self.position_id) is not str
            or not self.position_id.isascii()
            or not self.position_id.isdecimal()
            or self.position_id.startswith("0")
            or type(self.symbol) is not str
            or not self.symbol.endswith("_USDT")
            or type(self.filled_at_ts) is not int
            or self.filled_at_ts <= 0
            or not _positive(self.entry_price)
            or not _positive(self.native_stop_price)
            or not _nonnegative(self.best_profit_pct)
            or self.best_profit_pct > D("100")
            or type(self.trail_armed) is not bool
            or self.trail_armed != (self.best_profit_pct >= D(str(TRAIL_ACTIVATION_PCT)))
            or (self.last_completed_open_ts is not None and (
                type(self.last_completed_open_ts) is not int
                or self.last_completed_open_ts % BAR_SECONDS != 0
                or self.last_completed_open_ts < 0
            ))
            or self.native_stop_price > (
                self.entry_price * (D("1") + D(str(STOP_PCT))/D("100"))
                + self.entry_price * D("0.01")
            )
            and not self.trail_armed
        ):
            raise V40ExitPolicyHalted("FROZEN_V40_CHECKPOINT_ORIGIN_INVALID")


@dataclass(frozen=True)
class V40ExitDecision:
    status: STATUS
    proposed_checkpoint: V40ExitCheckpoint | None = None
    proposed_native_stop_price: D | None = None
    observed_best_profit_pct: D | None = None
    model_exit_price: D | None = None
    requires_venue_exit_reconciliation: bool = False
    requires_exchange_stop_amendment: bool = False
    may_persist_without_verified_exchange_readback: bool = False
    exchange_orders_sent: int = 0
    live_authorization: bool = False


def propose_next_completed_bar(
    previous: V40ExitCheckpoint,
    bar: V40CompletedBar,
    *,
    price_tick: D,
    now_ts: int,
) -> V40ExitDecision:
    """Read-only V40 policy projection for one fully post-fill Min30 bar.

    A candidate is NOT a stop amendment permit or durable checkpoint.
    It must be reconciled with an actual native stop on MEXC, including exact
    ownership and full-volume protection, BEFORE the next bar is processed.
    """
    if type(previous) is not V40ExitCheckpoint or type(bar) is not V40CompletedBar:
        raise V40ExitPolicyHalted("FROZEN_V40_CHECKPOINT_ORIGIN_INVALID")
    previous.validate()
    bar.validate(now_ts)
    if not _positive(price_tick):
        raise V40ExitPolicyHalted("FROZEN_V40_TICK_INVALID")

    first_ts = (
        (previous.filled_at_ts + BAR_SECONDS - 1) // BAR_SECONDS
    ) * BAR_SECONDS
    expected_ts = (
        first_ts if previous.last_completed_open_ts is None
        else previous.last_completed_open_ts + BAR_SECONDS
    )
    if bar.open_ts != expected_ts:
        raise V40ExitPolicyHalted("FROZEN_V40_GAP_OR_PRE_ENTRY_CANDLE_HALT")

    # ADVERSE FIRST. A touched native stop MUST be reconciled through
    # signed exchange order + fills + flat position evidence.
    if bar.high >= previous.native_stop_price:
        return V40ExitDecision(
            status="NATIVE_STOP_MAY_HAVE_TRIGGERED_RECONCILE_ONLY",
            model_exit_price=previous.native_stop_price,
            observed_best_profit_pct=previous.best_profit_pct,
            requires_venue_exit_reconciliation=True,
        )

    favorable_pct = (previous.entry_price - bar.low) / previous.entry_price * D("100")
    next_best = max(previous.best_profit_pct, favorable_pct)
    next_armed = previous.trail_armed or next_best >= D(str(TRAIL_ACTIVATION_PCT))
    next_stop = previous.native_stop_price
    if next_armed:
        raw_stop = previous.entry_price * (
            D("1") - (next_best - D(str(TRAIL_GAP_PCT))) / D("100")
        )
        tick_rounded = (raw_stop / price_tick).to_integral_value(
            rounding=ROUND_UP
        ) * price_tick
        next_stop = min(previous.native_stop_price, tick_rounded)

    # The new level may not have existed during this completed bar.
    # It takes effect only AFTER independently verified live stop amendment.
    proposal = replace(
        previous, native_stop_price=next_stop,
        best_profit_pct=next_best, trail_armed=next_armed,
        last_completed_open_ts=bar.open_ts,
    )
    if next_stop < previous.native_stop_price:
        # Never send a stop update already crossed by the current market.
        # Candle close is an additional conservative gate; a fresh signed
        # market price and spread check are still required at dispatch time.
        if next_stop <= bar.close + price_tick:
            raise V40ExitPolicyHalted("FROZEN_V40_PROPOSED_STOP_ALREADY_CROSSED")
        return V40ExitDecision(
            status="NATIVE_STOP_TIGHTENING_PROPOSED_UNAUTHORIZED",
            proposed_checkpoint=proposal,
            proposed_native_stop_price=next_stop,
            observed_best_profit_pct=next_best,
            requires_exchange_stop_amendment=True,
        )
    return V40ExitDecision(
        status="KEEP_VERIFIED_NATIVE_STOP",
        proposed_checkpoint=proposal,
        observed_best_profit_pct=next_best,
    )
