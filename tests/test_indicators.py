from app.indicators import atr, close_location, ema, pct_return, percentile_rank, upper_wick_ratio, zscore_last


def test_pct_return() -> None:
    assert pct_return(100, 125) == 0.25
    assert pct_return(0, 125) is None


def test_ema_tracks_latest_values() -> None:
    result = ema([1, 2, 3, 4, 5, 6], 3)
    assert result is not None
    assert 4 < result < 6


def test_atr_is_positive() -> None:
    result = atr([11, 12, 13, 14, 15, 16], [9, 10, 11, 12, 13, 14], [10, 11, 12, 13, 14, 15], 3)
    assert result is not None and result > 0


def test_zscore_last_detects_volume_spike() -> None:
    result = zscore_last([100.0] * 96 + [1000.0], 96)
    assert result is not None and result > 5


def test_candle_geometry() -> None:
    assert upper_wick_ratio(100, 120, 90, 105) == 0.5
    assert close_location(120, 90, 105) == 0.5


def test_percentile_rank() -> None:
    assert percentile_rank(4, [1, 2, 3, 4, 5]) == 0.8
