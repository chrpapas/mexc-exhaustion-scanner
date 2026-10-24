"""Strict VOL30/H4 execution *planning* using actual account and book snapshots.

Safe, deterministic and order-free. Does NOT implement the exact local V40
replay's feature filtering; historical parity requires original V40 sources.
One order plan is not an exchange fill. Reserving the cap and reconciling
order status must happen in the durable DB before/after every order.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from decimal import Decimal, ROUND_DOWN
from typing import Sequence

from app.t100_live_execution import displayed_vwap, adverse_bps, ExecutionHalted

D = Decimal
WINDOW = timedelta(minutes=30)
H4 = timedelta(minutes=120)
MAX_PARTICIPATION = D('0.05')
MAX_BOOK_AGE_MS = 1500
MAX_CAPTURE_LAG_MS = 2000
MAX_SLIPPAGE_BPS = D('100')


def positive(value: D, name: str, *, zero: bool = False) -> None:
    if not value.is_finite() or (value < 0 if zero else value <= 0):
        raise ValueError(f'{name} must be finite and {"nonnegative" if zero else "positive"}')


def utc(ts: datetime, name: str) -> datetime:
    if ts.tzinfo is None or ts.utcoffset() is None:
        raise ValueError(f'{name} missing timezone')
    return ts.astimezone(timezone.utc)


@dataclass(frozen=True)
class Vol30Parent:
    signal_id: int
    symbol: str
    tier: str
    admitted_at: datetime
    admitted_equity_usdt: D
    target_notional_usdt: D
    filled_notional_usdt: D
    pending_notional_usdt: D
    other_open_positions: int

    def __post_init__(self):
        if self.signal_id <= 0 or self.tier not in {'LOW_100', 'HIGH_225'}:
            raise ValueError('invalid parent signal or tier')
        if not self.symbol.endswith('_USDT') or self.symbol in {'ETH_USDT', 'BTC_USDT'}:
            raise ValueError('symbol not allowed')
        utc(self.admitted_at, 'admitted_at')
        positive(self.admitted_equity_usdt, 'admitted_equity_usdt')
        frozen_fraction = D('0.25') if self.tier == 'LOW_100' else D('0.5625')
        if self.target_notional_usdt > self.admitted_equity_usdt * frozen_fraction:
            raise ValueError('target exceeds frozen strategy tier allocation')
        for field in ('target_notional_usdt', 'filled_notional_usdt', 'pending_notional_usdt'):
            positive(getattr(self, field), field, zero=(field != 'target_notional_usdt'))
        if self.filled_notional_usdt + self.pending_notional_usdt > self.target_notional_usdt:
            raise ValueError('overcommitted parent')
        if not 0 <= self.other_open_positions <= 4:
            raise ValueError('four-position slot limit')


@dataclass(frozen=True)
class Vol30MarketSnapshot:
    observed_at: datetime
    book_exchange_at: datetime
    book_received_at: datetime
    turnover_window_end: datetime
    prior30_turnover_usdt: D
    bucket_committed_notional_usdt: D
    reference_price: D
    best_ask_price: D
    contract_size: D
    contract_step: D
    min_contracts: D
    max_contracts: D
    bids: Sequence[Sequence[str]]
    available_margin_usdt: D
    account_equity_usdt: D
    gross_exposure_usdt: D
    daily_loss_usdt: D
    max_gross_exposure_usdt: D
    max_child_notional_usdt: D

    def __post_init__(self):
        for field in ('observed_at', 'book_exchange_at', 'book_received_at', 'turnover_window_end'):
            utc(getattr(self, field), field)
        for field in ('prior30_turnover_usdt', 'bucket_committed_notional_usdt',
                      'available_margin_usdt', 'gross_exposure_usdt', 'daily_loss_usdt'):
            positive(getattr(self, field), field, zero=True)
        for field in ('reference_price', 'best_ask_price', 'contract_size', 'contract_step', 'min_contracts',
                      'max_contracts', 'account_equity_usdt', 'max_gross_exposure_usdt',
                      'max_child_notional_usdt'):
            positive(getattr(self, field), field)
        if self.max_contracts < self.min_contracts:
            raise ValueError('contract limits invalid')


@dataclass(frozen=True)
class ChildDecision:
    action: str  # SUBMIT_CHILD, DEFER, FINISHED, EXPIRED
    reason: str
    contracts: D = D(0)
    notional_usdt: D = D(0)
    adverse_bps: D | None = None
    remaining_target_usdt: D = D(0)
    bucket_available_usdt: D = D(0)
    expires_at: datetime | None = None


class Vol30H4Planner:
    """No order POST and no state mutations. Authoritative caps are caller snapshots."""
    def __init__(self, *, daily_loss_limit_usdt: D, max_slippage_bps: D = MAX_SLIPPAGE_BPS):
        positive(daily_loss_limit_usdt, 'daily_loss_limit_usdt')
        positive(max_slippage_bps, 'max_slippage_bps')
        if max_slippage_bps > D(100):
            raise ValueError('slippage cap cannot exceed frozen 100-bps scenario')
        self.daily_loss_limit_usdt = daily_loss_limit_usdt
        self.max_slippage_bps = max_slippage_bps

    def decide(self, parent: Vol30Parent, snap: Vol30MarketSnapshot) -> ChildDecision:
        now = utc(snap.observed_at, 'observed_at')
        admitted = utc(parent.admitted_at, 'admitted_at')
        expires_at = admitted + H4
        remaining = parent.target_notional_usdt - parent.filled_notional_usdt - parent.pending_notional_usdt
        budget = max(D(0), MAX_PARTICIPATION * snap.prior30_turnover_usdt - snap.bucket_committed_notional_usdt)

        def verdict(action: str, reason: str) -> ChildDecision:
            return ChildDecision(action, reason, remaining_target_usdt=remaining,
                                 bucket_available_usdt=budget, expires_at=expires_at)

        if now < admitted:
            raise ValueError('market snapshot is earlier than signal')
        if now >= expires_at:
            return verdict('EXPIRED', 'H4_120_MIN_WINDOW_EXPIRED')
        if remaining <= 0:
            return verdict('FINISHED', 'TARGET_ALREADY_FILLED_OR_PENDING')
        if parent.other_open_positions >= 4:
            return verdict('DEFER', 'ALL_FOUR_SLOTS_OCCUPIED')
        if snap.daily_loss_usdt >= self.daily_loss_limit_usdt:
            return verdict('DEFER', 'DAILY_LOSS_KILL_SWITCH')

        window = utc(snap.turnover_window_end, 'turnover_window_end')
        # Previous *completed* 30-minute candle only, not partly formed candle.
        # All execution attempts in the same next-30m bucket share a durable cap.
        unix = int(window.timestamp())
        if unix % 1800 != 0 or not window <= now < window + WINDOW:
            return verdict('DEFER', 'NO_FRESH_PREVIOUS_COMPLETED_30M_TURNOVER')
        exchange = utc(snap.book_exchange_at, 'book_exchange_at')
        received = utc(snap.book_received_at, 'book_received_at')
        if not exchange <= received <= now:
            return verdict('DEFER', 'ORDER_BOOK_CLOCK_INVALID')
        if (now - exchange).total_seconds() * 1000 > MAX_BOOK_AGE_MS:
            return verdict('DEFER', 'ORDER_BOOK_STALE')
        if (now - received).total_seconds() * 1000 > MAX_CAPTURE_LAG_MS:
            return verdict('DEFER', 'ORDER_BOOK_CAPTURE_LATE')
        if budget <= 0:
            return verdict('DEFER', 'VOL30_PARTICIPATION_EXHAUSTED')

        # 1x isolated margin constraint (do not assume 4 x 56.25% positions
        # can be simultaneously margined from only 100% of equity).
        free_portfolio = max(D(0), snap.max_gross_exposure_usdt - snap.gross_exposure_usdt)
        budget = min(budget, remaining, free_portfolio, snap.available_margin_usdt,
                     snap.max_child_notional_usdt)
        if budget <= 0:
            return verdict('DEFER', 'MARGIN_OR_ACCOUNT_EXPOSURE_EXHAUSTED')

        if snap.max_gross_exposure_usdt > snap.account_equity_usdt:
            return verdict('DEFER', 'GROSS_EXPOSURE_CAP_EXCEEDS_1X_EQUITY')
        if not snap.bids:
            return verdict('DEFER', 'NO_ORDER_BOOK_BIDS')
        bids: list[list[str]] = []
        prev = None
        depth_contracts = D(0)
        for level in snap.bids:
            if len(level) < 2:
                return verdict('DEFER', 'INVALID_BOOK_LEVEL')
            px, qty = D(str(level[0])), D(str(level[1]))
            if not px.is_finite() or not qty.is_finite() or px <= 0 or qty < 0:
                return verdict('DEFER', 'INVALID_BOOK_LEVEL')
            if prev is not None and px > prev:
                return verdict('DEFER', 'UNSORTED_BOOK_BIDS')
            prev = px
            depth_contracts += qty
            bids.append([str(px), str(qty)])

        if snap.best_ask_price < D(bids[0][0]):
            return verdict('DEFER', 'CROSSED_OR_INVALID_BOOK')
        spread_bps = (snap.best_ask_price-D(bids[0][0])) / ((snap.best_ask_price+D(bids[0][0]))/2) * D(10000)
        if spread_bps > D(15):
            return verdict('DEFER', 'SPREAD_EXCEEDS_15_BPS')

        # No guessing fills: displayed depth is a necessary but not sufficient
        # condition. The submit worker must re-check immediately before POST.
        step = snap.contract_step
        max_contracts_by_budget = budget / (snap.reference_price * snap.contract_size)
        cap = min(max_contracts_by_budget, snap.max_contracts, depth_contracts)
        lots = int((cap / step).to_integral_value(rounding=ROUND_DOWN))
        min_lots = int((snap.min_contracts / step).to_integral_value(rounding='ROUND_CEILING'))
        if lots < min_lots:
            return verdict('DEFER', 'CONTRACT_MINIMUM_OR_CAP')
        chosen = D(0)
        slippage = None
        # Optimal lot-quantized child notional bounded by worst displayed VWAP.
        left, right = min_lots, lots
        while left <= right:
            middle = (left + right) // 2
            qty = D(middle) * step
            try:
                vwap, _ = displayed_vwap(levels=bids, contract_size=snap.contract_size,
                                         requested_contracts=qty, side='sell')
            except (ExecutionHalted, ValueError):
                right = middle - 1
                continue
            adverse = adverse_bps(vwap, snap.reference_price, 'sell')
            if adverse <= self.max_slippage_bps:
                chosen, slippage = qty, adverse
                left = middle + 1
            else:
                right = middle - 1
        if chosen == 0:
            return verdict('DEFER', 'BOOK_DEPTH_OR_ADVERSE_SLIPPAGE_LIMIT')
        value = chosen * snap.contract_size * snap.reference_price
        return ChildDecision('SUBMIT_CHILD', 'VALID_LIQUIDITY_AND_RISK_GATES',
                             contracts=chosen, notional_usdt=value,
                             adverse_bps=slippage, remaining_target_usdt=remaining,
                             bucket_available_usdt=budget, expires_at=expires_at)