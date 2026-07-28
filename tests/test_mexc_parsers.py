from datetime import UTC, datetime

from app.mexc import parse_klines, parse_ticker


def test_parse_ticker() -> None:
    ticker = parse_ticker(
        {
            "symbol": "ABC_USDT",
            "lastPrice": 2.5,
            "bid1": 2.49,
            "ask1": 2.51,
            "amount24": 15_000_000,
            "volume24": 7_000_000,
            "holdVol": 123_000,
            "riseFallRate": 0.25,
            "timestamp": 1_700_000_000_000,
        }
    )
    assert ticker is not None
    assert ticker.symbol == "ABC_USDT"
    assert ticker.spread_pct is not None
    assert ticker.observed_at.tzinfo is UTC


def test_parse_klines() -> None:
    rows = parse_klines(
        "ABC_USDT",
        "Min15",
        {
            "time": [1_700_000_000],
            "open": [1.0],
            "high": [1.2],
            "low": [0.9],
            "close": [1.1],
            "vol": [1000],
            "amount": [1100],
        },
    )
    assert len(rows) == 1
    assert rows[0].open_time == datetime.fromtimestamp(1_700_000_000, UTC)
    assert rows[0].close == 1.1
