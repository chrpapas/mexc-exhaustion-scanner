"""Pure V40 VOL30_SKIP/H4 admission parity policy (no exchange calls).

Source-checked against locally supplied V39/V40 research:
* volatility_v28.py: 30% threshold on prior-24h close volatility
* preentry_v26.py: strictly prior 49 consecutive 30m candles
* ablation_v15.py: lower order-statistic P10/P25 prior-turnover
* exit_horizon_v31.py: H4 = 4-bar STRESSED EXIT SIZING HORIZON
* research_shock.py: pooled 5x shock-budget RESEARCH proxy
* contract_replay_v39.py: integer contract-lot correction

IMPORTANT: H4 IS NOT an entry execution campaign/window. V40's signal
admission and order entry are modeled at ONE candle price. This code is
for parity evaluation, not authorization to submit real orders.
"""
from __future__ import annotations

import math
import statistics
from dataclasses import dataclass
from decimal import Decimal, ROUND_CEILING, ROUND_FLOOR
from typing import Sequence

BAR_SECONDS = 1800
MAX_POSITIONS = 4
VOL_SKIP_THRESHOLD = 30.0


@dataclass(frozen=True)
class PriorBar:
    open_ts: int
    high: float
    close: float
    amount_usdt: float


@dataclass(frozen=True)
class V40Admission:
    action: str
    reason: str
    modeled_notional_usdt: float = 0.0
    desired_notional_usdt: float = 0.0
    equity_budget_usdt: float = 0.0
    max_entry_liquidity_usdt: float = 0.0
    stressed_exit_capacity_usdt: float = 0.0
    pre24_vol_pct: float | None = None
    tier_allocation_pct: float | None = None
    p25_turnover_usdt: float | None = None
    p10_turnover_usdt: float | None = None
    prior30_turnover_usdt: float | None = None


def _positive(x: float) -> bool:
    return math.isfinite(x) and x > 0


def _lower_quantile(values: Sequence[float], p: float) -> float:
    if not values:
        raise ValueError("no historical observations")
    ordered = sorted(float(x) for x in values)
    return ordered[max(0, min(len(ordered) - 1, math.ceil(p * len(ordered)) - 1))]


def causal_49_bar_features(bars: Sequence[PriorBar], *, admit_open_ts: int):
    """(24h volatility%, prior P25, prior P10, most recent 30m turnover).

    Volatility uses 48 newest-to-oldest consecutive log-close returns and
    sample standard deviation, exactly as the frozen V26 feature definition.
    """
    if len(bars) != 49 or admit_open_ts % BAR_SECONDS:
        return None
    bars = sorted(bars, key=lambda b: b.open_ts)
    for i, b in enumerate(bars):
        if b.open_ts != admit_open_ts - (49 - i) * BAR_SECONDS:
            return None
        if not all(_positive(float(x)) for x in (b.high, b.close, b.amount_usdt)):
            return None
        if b.close > b.high:
            return None
    latest = bars[::-1]
    returns = [math.log(latest[i].close / latest[i + 1].close) for i in range(48)]
    volatility = round(statistics.stdev(returns) * math.sqrt(48) * 100, 6)
    amounts = [b.amount_usdt for b in bars[-48:]]
    return volatility, _lower_quantile(amounts, .25), _lower_quantile(amounts, .10), amounts[-1]


def shock_budget(*, equity_usdt: float, marked_existing_usdt: float, cost_bps: float) -> float:
    """Frozen V40 research 5x pooled-margin 20% shock-buffer model.

    5x here is a HISTORICAL RISK-ASSUMPTION parameter, not a live leverage setting.
    """
    b = .20 + 1.20 / (5. * .95)
    fee_and_slip = .0008 + cost_bps / 10000.
    return max(0., (equity_usdt - b * marked_existing_usdt) / (b + fee_and_slip))


def admission(*, prior_bars: Sequence[PriorBar], admission_open_ts: int,
              equity_usdt: float, marked_existing_usdt: float,
              stage1_atr7_pct: float, active_positions: int,
              already_open_symbol: bool, used_same_bar_usdt: float = 0.,
              cost_bps: float = 100.) -> V40Admission:
    if cost_bps not in (100., 200.):
        raise ValueError("V40 only replayed 100/200 bps here")
    for name, x in (("equity", equity_usdt), ("marked", marked_existing_usdt),
                    ("atr", stage1_atr7_pct), ("used", used_same_bar_usdt)):
        if not math.isfinite(x) or (x < 0 if name in ("marked", "used") else x <= 0):
            raise ValueError("invalid " + name)
    if already_open_symbol:
        return V40Admission("REJECT", "duplicate_symbol")
    if active_positions < 0:
        raise ValueError("invalid position count")
    if active_positions >= MAX_POSITIONS:
        return V40Admission("REJECT", "four_slot_limit")
    if stage1_atr7_pct < 4.0:
        return V40Admission("REJECT", "below_ATR4_candidate_floor")
    features = causal_49_bar_features(prior_bars, admit_open_ts=admission_open_ts)
    if features is None:
        return V40Admission("MISSING", "causal_49_bar_history_incomplete")
    vol, p25, p10, prior30 = features
    if vol >= VOL_SKIP_THRESHOLD:
        return V40Admission("SKIP", "vol_ge_30",
                            pre24_vol_pct=vol, p25_turnover_usdt=p25,
                            p10_turnover_usdt=p10, prior30_turnover_usdt=prior30)

    # V40 spec: low_gross=100%, high_gross=250%, divided across 4 slots:
    # LOW=25%, HIGH=62.5% target, BEFORE shock and turnover clipping.
    gross_pct = 250. if stage1_atr7_pct >= 5.7 else 100.
    desired = equity_usdt * gross_pct / (100. * MAX_POSITIONS)
    budget = shock_budget(equity_usdt=equity_usdt,
                          marked_existing_usdt=marked_existing_usdt, cost_bps=cost_bps)
    # H4 is an exit capacity ceiling, not a 120 minute entry order window.
    exit_bound = .05 * p10 * 4 * .50 / 1.75
    entry_bound = min(.05 * prior30, .05 * p25, exit_bound)
    free = max(0., .05 * prior30 - used_same_bar_usdt)
    modeled = min(desired, budget, free, entry_bound)
    diagnostic = dict(desired_notional_usdt=desired, equity_budget_usdt=budget,
                      max_entry_liquidity_usdt=entry_bound,
                      stressed_exit_capacity_usdt=exit_bound,
                      pre24_vol_pct=vol,
                      tier_allocation_pct=gross_pct / MAX_POSITIONS,
                      p25_turnover_usdt=p25, p10_turnover_usdt=p10,
                      prior30_turnover_usdt=prior30)
    if modeled <= 1e-8:
        return V40Admission("REJECT", "no_admissible_turnover_or_shock_budget", **diagnostic)
    return V40Admission("ADMIT", "modeled_single_bar_admission",
                        modeled_notional_usdt=modeled, **diagnostic)


def lot_quantize_v39(*, modeled_notional: float, entry_price: float,
                     contract_size: str, volume_step: str,
                     min_volume: str, max_volume: str | None):
    """V39 rounding and same-price order child count; NOT real child fills."""
    size, step, minimum = map(Decimal, (contract_size, volume_step, min_volume))
    maximum = Decimal(max_volume) if max_volume is not None else None
    price, notional = Decimal(str(entry_price)), Decimal(str(modeled_notional))
    if any(v <= 0 or not v.is_finite() for v in (size, step, minimum, price, notional)):
        raise ValueError("invalid contract specification")
    if maximum is not None and (not maximum.is_finite() or maximum < minimum):
        raise ValueError("invalid maximum volume")
    units = int((notional / (size * price) / step).to_integral_value(rounding=ROUND_FLOOR))
    volume = units * step
    min_units = int((minimum / step).to_integral_value(rounding=ROUND_CEILING))
    if units < min_units:
        return Decimal(0), Decimal(0), 0
    if maximum is None:
        children = 1
    else:
        max_units = int((maximum / step).to_integral_value(rounding=ROUND_FLOOR))
        if max_units < min_units:
            return Decimal(0), Decimal(0), 0
        children = (units + max_units - 1) // max_units
        if units < children * min_units:
            return Decimal(0), Decimal(0), 0
    return volume, volume * size * price, children
