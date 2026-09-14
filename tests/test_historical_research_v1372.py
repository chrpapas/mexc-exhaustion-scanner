import asyncio
from datetime import UTC, datetime, timedelta
from pathlib import Path

import app.historical_research as hr


def test_cache_size_walk_is_not_repeated_per_job(monkeypatch, tmp_path):
    calls = {"size": 0}

    def fake_size(_path):
        calls["size"] += 1
        return 0

    monkeypatch.setattr(hr, "_cache_size_bytes", fake_size)
    monkeypatch.setattr(hr, "_discover_universe", lambda client, config: asyncio.sleep(0, result=["BTC_USDT"]))

    async def fake_chunk(self, symbol, interval, start, end):
        return {"time": [int(start.timestamp())], "open": [1], "high": [1], "low": [1], "close": [1], "vol": [1], "amount": [1]}

    monkeypatch.setattr(hr.HistoricalPublicClient, "kline_chunk", fake_chunk)
    monkeypatch.setattr(hr.HistoricalPublicClient, "close", lambda self: asyncio.sleep(0))

    start = datetime(2026, 1, 1, tzinfo=UTC)
    config = hr.HistoricalFetchConfig(
        cache_dir=tmp_path,
        start=start,
        end=start + timedelta(hours=10),
        intervals=("Min15",),
        requests_per_second=1.0,
        max_runtime_minutes=1.0,
        max_cache_gb=10.0,
        chunk_candles=4,
    )
    asyncio.run(hr.fetch_history(config))
    assert calls["size"] == 1
