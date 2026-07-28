from __future__ import annotations

import math
from collections.abc import Sequence


def pct_return(start: float, end: float) -> float | None:
    if start <= 0:
        return None
    return end / start - 1.0


def ema(values: Sequence[float], period: int) -> float | None:
    if period <= 0 or len(values) < period:
        return None
    alpha = 2.0 / (period + 1.0)
    result = sum(values[:period]) / period
    for value in values[period:]:
        result = alpha * value + (1.0 - alpha) * result
    return result


def atr(
    highs: Sequence[float],
    lows: Sequence[float],
    closes: Sequence[float],
    period: int = 14,
) -> float | None:
    if not (len(highs) == len(lows) == len(closes)) or len(closes) < period + 1:
        return None
    true_ranges = [
        max(
            highs[index] - lows[index],
            abs(highs[index] - closes[index - 1]),
            abs(lows[index] - closes[index - 1]),
        )
        for index in range(1, len(closes))
    ]
    result = sum(true_ranges[:period]) / period
    for value in true_ranges[period:]:
        result = ((period - 1) * result + value) / period
    return result


def zscore_last(values: Sequence[float], lookback: int = 96) -> float | None:
    if lookback < 2 or len(values) < lookback + 1:
        return None
    history = list(values[-(lookback + 1) : -1])
    current = values[-1]
    mean = sum(history) / len(history)
    variance = sum((value - mean) ** 2 for value in history) / len(history)
    standard_deviation = math.sqrt(variance)
    if standard_deviation == 0:
        if current == mean:
            return 0.0
        return 10.0 if current > mean else -10.0
    return (current - mean) / standard_deviation


def percentile_rank(value: float, universe: Sequence[float]) -> float | None:
    clean = sorted(item for item in universe if math.isfinite(item))
    if not clean:
        return None
    return sum(1 for item in clean if item <= value) / len(clean)


def upper_wick_ratio(open_: float, high: float, low: float, close: float) -> float | None:
    candle_range = high - low
    if candle_range <= 0:
        return None
    return (high - max(open_, close)) / candle_range


def close_location(high: float, low: float, close: float) -> float | None:
    candle_range = high - low
    if candle_range <= 0:
        return None
    return (close - low) / candle_range
