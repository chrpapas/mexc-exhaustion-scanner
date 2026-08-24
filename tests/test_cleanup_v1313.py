from __future__ import annotations

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
import sys
import types

import pytest

sys.modules.setdefault("asyncpg", types.SimpleNamespace())

from app.models import Candle, Ticker
from app.performance import build_performance_summary
from app.signals import ExecutionRisk
from app.worker import ScannerWorker


def _candles(symbol: str, interval: str, count: int, step: timedelta) -> list[Candle]:
    end = datetime.now(UTC) - step
    start = end - step * (count - 1)
    out = []
    for i in range(count):
        t = start + step * i
        price = 1.0 + i * 0.0005
        out.append(Candle(symbol, interval, t, price, price * 1.01, price * 0.99, price, 1000.0, 1000.0))
    return out


@pytest.mark.asyncio
async def test_extreme_risk_is_suppressed_before_episode_or_signal_pipeline(monkeypatch):
    worker = ScannerWorker.__new__(ScannerWorker)
    worker.settings = SimpleNamespace(
        excluded_symbols=frozenset(),
        signal_eval_concurrency=1,
        signal_eval_progress_every=1,
        diagnostic_symbols=frozenset(),
        high_risk_min_amount_24h=500_000.0,
        high_risk_max_spread_pct=2.0,
    )
    worker.thresholds = object()
    symbol = "EXTREME_USDT"
    btc = Ticker("BTC_USDT", datetime.now(UTC), 1.0, 0.999, 1.001, 1_000_000, 1_000_000, 1_000_000, 0.9, 1.1, 0.01, 1.0, 1.0, 0.0)
    token = Ticker(symbol, datetime.now(UTC), 1.2, 1.19, 1.21, 100_000, 100_000, 100_000, 0.5, 1.3, 0.50, 1.2, 1.2, 0.0)
    worker.latest_tickers = {"BTC_USDT": btc, symbol: token}

    class Db:
        async def fetch_candles(self, sym: str, interval: str, limit: int):
            if interval == "Min15":
                return _candles(sym, interval, 300, timedelta(minutes=15))
            return _candles(sym, interval, 80, timedelta(hours=4))

        async def get_active_episode(self, sym: str):  # pragma: no cover - must never be reached
            raise AssertionError("EXTREME_RISK entered episode state machine")

        async def insert_signal(self, signal):  # pragma: no cover - must never be reached
            raise AssertionError("EXTREME_RISK was persisted as a signal")

    worker.db = Db()

    async def discovery_symbols(*, include_benchmark: bool):
        return [symbol]

    worker.discovery_symbols = discovery_symbols
    monkeypatch.setattr("app.worker.score_run", lambda *a, **k: (6, ["synthetic"], True))
    monkeypatch.setattr(
        "app.worker.classify_execution_risk",
        lambda *a, **k: ExecutionRisk("extreme_risk", False, "suppressed", ("synthetic",)),
    )

    await worker.evaluate_signals()


def test_public_strategy_summaries_are_tp5_and_standard_7d_only():
    confirmed = datetime(2026, 8, 1, tzinfo=UTC)
    rows = [
        {
            "episode_id": 1,
            "symbol": "STD_USDT",
            "risk_tier": "standard",
            "confirmed_at": confirmed,
            "entry_price": 1.0,
            "current_return_pct": 0.40,
            "return_168h_pct": 0.40,
            "matured_168h_at": confirmed + timedelta(days=7),
            "target_5_at": confirmed + timedelta(hours=2),
            "path_mae_before_target_5": -0.08,
        },
        {
            "episode_id": 2,
            "symbol": "HIGH_USDT",
            "risk_tier": "high_risk",
            "confirmed_at": confirmed + timedelta(hours=1),
            "entry_price": 1.0,
            "current_return_pct": -0.20,
            "return_168h_pct": -0.20,
            "matured_168h_at": confirmed + timedelta(days=7, hours=1),
            "target_5_at": confirmed + timedelta(hours=4),
            "path_mae_before_target_5": -0.04,
        },
        {
            "episode_id": 3,
            "symbol": "EXTREME_USDT",
            "risk_tier": "extreme_risk",
            "confirmed_at": confirmed,
            "entry_price": 1.0,
            "current_return_pct": -0.90,
            "return_168h_pct": -0.90,
            "matured_168h_at": confirmed + timedelta(days=7),
            "target_5_at": None,
        },
    ]
    report = build_performance_summary(rows, now_utc=confirmed + timedelta(days=10), timezone_name="Europe/Zurich")
    assert report.tp5_public.matured_7d == 2
    assert report.tp5_public.hits_7d == 2
    assert report.tp5_public.hit_rate_7d == 1.0
    assert report.standard_7d_public.matured_7d == 1
    assert report.standard_7d_public.avg_return == pytest.approx(0.40)
    assert report.standard_7d_public.positive_rate == 1.0
