from __future__ import annotations

import asyncio
import logging
import sys
import types
from datetime import UTC, datetime
from types import SimpleNamespace

import pytest

sys.modules.setdefault("asyncpg", types.SimpleNamespace())

from app.models import Ticker
from app.worker import ScannerWorker


def _ticker(symbol: str) -> Ticker:
    return Ticker(
        symbol=symbol,
        observed_at=datetime.now(UTC),
        last_price=1.0,
        bid1=0.999,
        ask1=1.001,
        amount24=10_000_000.0,
        volume24=10_000_000.0,
        hold_vol=1_000_000.0,
        low24=0.8,
        high24=1.2,
        rise_fall_rate=0.10,
        index_price=1.0,
        fair_price=1.0,
        funding_rate=0.0,
    )


@pytest.mark.asyncio
async def test_signal_evaluation_runs_concurrently_and_logs_progress(caplog):
    worker = ScannerWorker.__new__(ScannerWorker)
    symbols = [f"T{i}_USDT" for i in range(8)]
    worker.settings = SimpleNamespace(
        excluded_symbols=frozenset(),
        signal_eval_concurrency=4,
        signal_eval_progress_every=2,
        diagnostic_symbols=frozenset(),
    )
    worker.latest_tickers = {
        "BTC_USDT": _ticker("BTC_USDT"),
        **{symbol: _ticker(symbol) for symbol in symbols},
    }

    class FakeDb:
        def __init__(self) -> None:
            self.active = 0
            self.max_active = 0

        async def fetch_candles(self, symbol: str, interval: str, limit: int):
            del symbol, interval, limit
            self.active += 1
            self.max_active = max(self.max_active, self.active)
            try:
                await asyncio.sleep(0.02)
                return []
            finally:
                self.active -= 1

    worker.db = FakeDb()

    async def discovery_symbols(*, include_benchmark: bool):
        assert include_benchmark is False
        return list(symbols)

    worker.discovery_symbols = discovery_symbols

    caplog.set_level(logging.INFO)
    await worker.evaluate_signals()

    # Four symbol workers each fetch two intervals concurrently.
    assert worker.db.max_active >= 4
    text = caplog.text
    assert "Signal evaluation started: symbols=8 concurrency=4" in text
    assert "Signal evaluation progress: 8/8 failures=0" in text
    assert "Signal evaluation complete: symbols=8 evaluated=0" in text


@pytest.mark.asyncio
async def test_signal_evaluation_isolates_single_symbol_failure(caplog):
    worker = ScannerWorker.__new__(ScannerWorker)
    symbols = ["BAD_USDT", "GOOD_USDT"]
    worker.settings = SimpleNamespace(
        excluded_symbols=frozenset(),
        signal_eval_concurrency=2,
        signal_eval_progress_every=1,
        diagnostic_symbols=frozenset(),
    )
    worker.latest_tickers = {
        "BTC_USDT": _ticker("BTC_USDT"),
        **{symbol: _ticker(symbol) for symbol in symbols},
    }

    class FakeDb:
        async def fetch_candles(self, symbol: str, interval: str, limit: int):
            del interval, limit
            await asyncio.sleep(0)
            if symbol == "BAD_USDT":
                raise RuntimeError("synthetic db failure")
            return []

    worker.db = FakeDb()

    async def discovery_symbols(*, include_benchmark: bool):
        assert include_benchmark is False
        return list(symbols)

    worker.discovery_symbols = discovery_symbols

    caplog.set_level(logging.INFO)
    await worker.evaluate_signals()

    text = caplog.text
    assert "Signal evaluation failed symbol=BAD_USDT" in text
    assert "Signal evaluation progress: 2/2 failures=1" in text
    assert "Signal evaluation complete: symbols=2 evaluated=0" in text
    assert "failures=1" in text
