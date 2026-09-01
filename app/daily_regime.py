from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any, Iterable

from app.indicators import atr, ema, pct_return

DAILY_REGIME_V1_VERSION = "daily_bull_regime_v1"
DAILY_REGIME_REQUIRED_FEATURES = (
    "daily_close_above_ema20",
    "daily_ema20_slope",
    "daily_momentum_3d",
)


def _number(value: object) -> float | None:
    try:
        return float(value) if value is not None else None
    except (TypeError, ValueError):
        return None


def reconstruct_daily_regime_features(
    *,
    confirmed_at: datetime,
    entry_price: float,
    day1_rows: Iterable[dict[str, Any]],
) -> tuple[dict[str, Any], dict[str, str]]:
    """Reconstruct causal 1D regime features using completed daily candles only."""
    completed = sorted(
        (
            dict(row)
            for row in day1_rows
            if isinstance(row.get("open_time"), datetime)
            and row["open_time"] + timedelta(days=1) <= confirmed_at
        ),
        key=lambda row: row["open_time"],
    )[-45:]

    recovered: dict[str, Any] = {}
    sources: dict[str, str] = {}
    if len(completed) < 21:
        return recovered, sources

    closes = [_number(row.get("close")) for row in completed]
    highs = [_number(row.get("high")) for row in completed]
    lows = [_number(row.get("low")) for row in completed]
    if not all(value is not None for value in closes + highs + lows):
        return recovered, sources

    clean_closes = [float(value) for value in closes if value is not None]
    clean_highs = [float(value) for value in highs if value is not None]
    clean_lows = [float(value) for value in lows if value is not None]

    ema20_now = ema(clean_closes, 20)
    ema20_prev = ema(clean_closes[:-1], 20) if len(clean_closes) >= 21 else None
    atr14 = atr(clean_highs, clean_lows, clean_closes, 14)
    if ema20_now is None or ema20_prev is None:
        return recovered, sources

    last_close = clean_closes[-1]
    recovered["daily_close_above_ema20"] = bool(last_close > ema20_now)
    recovered["daily_ema20_slope"] = pct_return(ema20_prev, ema20_now)
    if len(clean_closes) >= 4:
        recovered["daily_momentum_3d"] = pct_return(clean_closes[-4], clean_closes[-1])
    if len(clean_closes) >= 8:
        recovered["daily_momentum_7d"] = pct_return(clean_closes[-8], clean_closes[-1])
    if atr14 is not None and atr14 > 0 and entry_price > 0:
        recovered["daily_distance_above_ema20_atr"] = (entry_price - ema20_now) / atr14
    if len(clean_highs) >= 2:
        recovered["daily_higher_high"] = clean_highs[-1] > clean_highs[-2]
        recovered["daily_higher_low"] = clean_lows[-1] > clean_lows[-2]
    recovered["daily_last_completed_close"] = last_close
    sources.update({key: "day1_reconstruction" for key in recovered})
    return recovered, sources


def daily_regime_state(snapshot: dict[str, Any]) -> bool | None:
    close_above = snapshot.get("daily_close_above_ema20")
    slope = _number(snapshot.get("daily_ema20_slope"))
    momentum_3d = _number(snapshot.get("daily_momentum_3d"))
    if not isinstance(close_above, bool) or slope is None or momentum_3d is None:
        return None
    return close_above and slope > 0 and momentum_3d > 0


def daily_regime_snapshot_metadata(snapshot: dict[str, Any]) -> dict[str, Any]:
    state = daily_regime_state(snapshot)
    missing = [
        key for key in DAILY_REGIME_REQUIRED_FEATURES
        if snapshot.get(key) is None
    ]
    return {
        "daily_regime_v1_version": DAILY_REGIME_V1_VERSION,
        "daily_regime_v1_computable": state is not None,
        "daily_regime_v1_bullish": state,
        "daily_regime_v1_missing_fields": missing,
    }
