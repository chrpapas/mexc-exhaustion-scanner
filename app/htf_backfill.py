from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any, Iterable

from app.indicators import atr, ema, pct_return


def _number(value: object) -> float | None:
    try:
        parsed = float(value) if value is not None else None
    except (TypeError, ValueError):
        return None
    return parsed


def _completed_rows(
    rows: Iterable[dict[str, Any]],
    *,
    confirmed_at: datetime,
    candle_minutes: int,
) -> list[dict[str, Any]]:
    delta = timedelta(minutes=candle_minutes)
    completed = [
        dict(row)
        for row in rows
        if isinstance(row.get("open_time"), datetime)
        and row["open_time"] + delta <= confirmed_at
    ]
    return sorted(completed, key=lambda row: row["open_time"])


def _contiguous_tail(rows: list[dict[str, Any]], *, count: int, minutes: int) -> list[dict[str, Any]]:
    if len(rows) < count:
        return []
    tail = rows[-count:]
    expected = timedelta(minutes=minutes)
    if any(
        tail[index]["open_time"] - tail[index - 1]["open_time"] != expected
        for index in range(1, len(tail))
    ):
        return []
    return tail


def reconstruct_candle_htf_features(
    *,
    confirmed_at: datetime,
    entry_price: float,
    min15_rows: Iterable[dict[str, Any]],
    hour4_rows: Iterable[dict[str, Any]],
) -> tuple[dict[str, float], dict[str, str]]:
    """Reconstruct causal HTF V1 inputs from candles available before a signal.

    The function intentionally reconstructs only candle-derived fields. Historical
    cross-sectional percentile is not synthesized because the exact full MEXC
    universe used by the live scanner is not guaranteed to have been persisted.

    ``return_24h`` is a fallback proxy when the exact historical ticker 24h return
    is unavailable. The source map marks it explicitly as ``min15_24h_proxy``.
    """
    recovered: dict[str, float] = {}
    sources: dict[str, str] = {}

    min15 = _completed_rows(min15_rows, confirmed_at=confirmed_at, candle_minutes=15)
    tail9 = _contiguous_tail(min15, count=9, minutes=15)
    if tail9:
        value = pct_return(_number(tail9[0].get("close")) or 0.0, _number(tail9[4].get("close")) or 0.0)
        if value is not None:
            recovered["previous_momentum_1h"] = value
            sources["previous_momentum_1h"] = "min15_reconstruction"

    reference_cutoff = confirmed_at - timedelta(hours=24)
    reference = None
    for row in min15:
        close_at = row["open_time"] + timedelta(minutes=15)
        if close_at <= reference_cutoff:
            reference = row
        else:
            break
    reference_close = _number(reference.get("close")) if reference is not None else None
    if entry_price > 0 and reference_close is not None and reference_close > 0:
        value = pct_return(reference_close, entry_price)
        if value is not None:
            recovered["return_24h"] = value
            sources["return_24h"] = "min15_24h_proxy"

    hour4 = _completed_rows(hour4_rows, confirmed_at=confirmed_at, candle_minutes=240)
    # Live evaluation fetches at most 80 4h candles, so mirror that tail exactly.
    hour4 = hour4[-80:]
    closes = [_number(row.get("close")) for row in hour4]
    highs = [_number(row.get("high")) for row in hour4]
    lows = [_number(row.get("low")) for row in hour4]
    if (
        len(hour4) >= 25
        and all(value is not None for value in closes)
        and all(value is not None for value in highs)
        and all(value is not None for value in lows)
    ):
        clean_closes = [float(value) for value in closes if value is not None]
        clean_highs = [float(value) for value in highs if value is not None]
        clean_lows = [float(value) for value in lows if value is not None]
        ema20 = ema(clean_closes, 20)
        atr14 = atr(clean_highs, clean_lows, clean_closes, 14)
        if ema20 is not None and atr14 is not None and atr14 > 0 and entry_price > 0:
            recovered["distance_above_ema20_atr_4h"] = (entry_price - ema20) / atr14
            sources["distance_above_ema20_atr_4h"] = "hour4_reconstruction"

    return recovered, sources
