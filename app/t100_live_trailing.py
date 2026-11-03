"""Frozen T100 short trailing calculation; not yet connected to a live stop manager."""
from __future__ import annotations
from dataclasses import dataclass
from decimal import Decimal, ROUND_UP

@dataclass(frozen=True)
class TrailStep:
    active: bool
    best_profit_pct: Decimal
    stop_price: Decimal
    changed: bool

def next_short_stop(*, entry: Decimal, low: Decimal, current_stop: Decimal,
                    best_profit_pct: Decimal, price_tick: Decimal,
                    armed: bool) -> TrailStep:
    if any(not v.is_finite() or v <= 0 for v in (entry, low, current_stop, price_tick)):
        raise ValueError("prices and tick must be positive and finite")
    if not best_profit_pct.is_finite():
        raise ValueError("best return invalid")
    observed = (entry - low) / entry * Decimal(100)
    best = max(best_profit_pct, observed)
    active = armed or best >= Decimal(14)
    if not active:
        return TrailStep(False, best, current_stop, False)
    requested = entry * (Decimal(1) - (best - Decimal("0.5")) / Decimal(100))
    rounded = (requested / price_tick).to_integral_value(rounding=ROUND_UP) * price_tick
    target = min(current_stop, rounded)
    return TrailStep(True, best, target, target < current_stop)
