from __future__ import annotations

import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path

from app.historical_live_store import SpreadProxyModel, calibrate_spread_proxy, derive_ticker_snapshots
from app.models import Candle


def candle(symbol: str, minute: int, price: float, amount: float = 100_000.0) -> Candle:
    t = datetime(2026, 1, 1, tzinfo=UTC) + timedelta(minutes=minute)
    return Candle(symbol, "Min5", t, price, price * 1.01, price * 0.99, price, amount / price, amount)


def test_derived_ticker_uses_live_shape_and_causal_24h_window():
    rows = [candle("X_USDT", i * 5, 100 + i, 20_000.0) for i in range(300)]
    out = list(derive_ticker_snapshots("X_USDT", rows, spread_model=SpreadProxyModel()))
    last = out[-1]
    assert last.symbol == "X_USDT"
    assert last.observed_at == rows[-1].open_time + timedelta(minutes=5)
    assert last.last_price == rows[-1].close
    assert last.amount24 <= 288 * 20_000.0 + 1e-6
    assert last.bid1 is not None and last.ask1 is not None
    assert last.spread_pct is not None


def test_spread_proxy_preserves_amount_band_tiers():
    m = SpreadProxyModel()
    assert m.tier(4_000_000) == "standard"
    assert m.tier(1_000_000) == "high_risk"
    assert m.tier(100_000) == "extreme_risk"


def test_spread_calibration_reports_tier_accuracy(tmp_path: Path):
    db_path = tmp_path / "ticker.sqlite"
    db = sqlite3.connect(db_path)
    db.execute(
        """CREATE TABLE ticker_snapshots(
        symbol TEXT, observed_at TEXT, amount24 REAL, spread_pct REAL
        )"""
    )
    rows = [
        ("A_USDT", "2026-01-01 00:00:00+00", 4_000_000, 0.02),
        ("B_USDT", "2026-01-01 00:00:00+00", 1_000_000, 0.10),
        ("C_USDT", "2026-01-01 00:00:00+00", 100_000, 2.00),
    ]
    db.executemany("INSERT INTO ticker_snapshots VALUES (?,?,?,?)", rows)
    db.commit(); db.close()
    result = calibrate_spread_proxy(db_path)
    assert result.accuracy == 1.0
    assert result.standard_recall == 1.0
    assert result.high_risk_recall == 1.0
    assert result.extreme_recall == 1.0
