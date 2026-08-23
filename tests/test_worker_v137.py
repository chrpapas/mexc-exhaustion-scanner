from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from app.worker import ScannerWorker


@pytest.mark.asyncio
async def test_v137_sync_interval_backfills_left_edge_even_when_wide_scan_seeded_recent_history():
    worker = ScannerWorker.__new__(ScannerWorker)
    now = datetime.now(UTC)
    earliest = now - timedelta(days=4)
    latest = now - timedelta(hours=4)
    desired = now - timedelta(days=90)

    class FakeDb:
        def __init__(self):
            self.upserts = []

        async def earliest_candle_time(self, symbol, interval):
            assert symbol == "ZEC_USDT"
            assert interval == "Hour4"
            return earliest

        async def latest_candle_time(self, symbol, interval):
            return latest

        async def upsert_candles(self, candles):
            self.upserts.append(candles)

    class FakeMexc:
        def __init__(self):
            self.calls = []

        async def get_klines(self, symbol, interval, start_seconds, end_seconds=None):
            self.calls.append((symbol, interval, start_seconds, end_seconds))
            return [len(self.calls)]

    worker.db = FakeDb()
    worker.mexc = FakeMexc()

    await worker._sync_interval_from(
        "ZEC_USDT", "Hour4", desired_start=desired, overlap_hours=12
    )

    assert len(worker.mexc.calls) == 2
    history_call, recent_call = worker.mexc.calls
    assert history_call[2] == int(desired.timestamp())
    assert history_call[3] == int((earliest - timedelta(seconds=1)).timestamp())
    assert recent_call[2] == int((latest - timedelta(hours=12)).timestamp())
    assert recent_call[3] is None
    assert len(worker.db.upserts) == 2
