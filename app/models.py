from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any


@dataclass(frozen=True, slots=True)
class Candle:
    symbol: str
    interval: str
    open_time: datetime
    open: float
    high: float
    low: float
    close: float
    volume: float
    amount: float


@dataclass(frozen=True, slots=True)
class Ticker:
    symbol: str
    observed_at: datetime
    last_price: float
    bid1: float | None
    ask1: float | None
    amount24: float
    volume24: float
    hold_vol: float | None
    low24: float | None
    high24: float | None
    rise_fall_rate: float
    index_price: float | None
    fair_price: float | None
    funding_rate: float | None

    @property
    def spread_pct(self) -> float | None:
        if self.bid1 is None or self.ask1 is None or self.bid1 <= 0 or self.ask1 <= 0:
            return None
        midpoint = (self.bid1 + self.ask1) / 2.0
        if midpoint <= 0:
            return None
        return (self.ask1 - self.bid1) / midpoint * 100.0


@dataclass(frozen=True, slots=True)
class RunSignal:
    symbol: str
    signaled_at: datetime
    level: str
    score: int
    features: dict[str, Any]
    reasons: list[str]
