from __future__ import annotations

import logging
import sys
import types
from contextlib import asynccontextmanager
from types import SimpleNamespace

import pytest

sys.modules.setdefault("asyncpg", types.SimpleNamespace())

from app.db import Database
from app.worker import ScannerWorker


@pytest.mark.asyncio
async def test_research_snapshot_sync_returns_insert_count():
    class FakePool:
        async def execute(self, query: str):
            assert "INSERT INTO research_signal_features" in query
            assert "FROM run_signals" in query
            assert "confirmed_short" in query
            return "INSERT 0 4"

    db = Database("postgresql://unused")
    db._pool = FakePool()
    assert await db.sync_research_signal_snapshots() == 4


@pytest.mark.asyncio
async def test_research_path_sync_is_bounded_and_uses_local_statement_timeout():
    calls: list[tuple[str, tuple[object, ...]]] = []

    class FakeTransaction:
        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            return False

    class FakeConn:
        def transaction(self):
            return FakeTransaction()

        async def execute(self, query: str, *args):
            calls.append((query, args))
            return "SELECT 1"

        async def fetchval(self, query: str, *args):
            calls.append((query, args))
            assert "LIMIT $1" in query
            assert "research_signal_path_15m" in query
            assert "LEFT JOIN candles btc" in query
            return 321

    class FakeAcquire:
        async def __aenter__(self):
            return FakeConn()

        async def __aexit__(self, exc_type, exc, tb):
            return False

    class FakePool:
        def acquire(self):
            return FakeAcquire()

    db = Database("postgresql://unused")
    db._pool = FakePool()
    inserted = await db.sync_research_signal_paths(
        batch_rows=2000,
        horizon_hours=168,
        statement_timeout_seconds=10,
    )
    assert inserted == 321
    assert calls[0][1] == ("10s",)
    assert calls[1][1] == (2000, 168)


@pytest.mark.asyncio
async def test_worker_research_loop_uses_only_database_and_logs_summary(caplog):
    worker = ScannerWorker.__new__(ScannerWorker)
    worker.settings = SimpleNamespace(
        research_path_batch_rows=2000,
        research_path_horizon_hours=168,
        research_db_timeout_seconds=10,
    )

    class FakeDb:
        async def sync_research_signal_snapshots(self):
            return 2

        async def sync_research_signal_paths(self, **kwargs):
            assert kwargs == {
                "batch_rows": 2000,
                "horizon_hours": 168,
                "statement_timeout_seconds": 10,
            }
            return 500

    worker.db = FakeDb()
    caplog.set_level(logging.INFO)
    await worker.sync_research_data()
    assert "Research sync: snapshots=2 path_rows=500 batch_cap=2000 horizon=168h" in caplog.text


def test_research_migration_keeps_expensive_analytics_in_query_time_view():
    from pathlib import Path

    sql = (Path(__file__).resolve().parents[1] / "migrations" / "013_research_signal_paths.sql").read_text()
    assert "CREATE TABLE IF NOT EXISTS research_signal_features" in sql
    assert "CREATE TABLE IF NOT EXISTS research_signal_path_15m" in sql
    assert "CREATE OR REPLACE VIEW research_signal_features_enriched" in sql
    assert "CREATE OR REPLACE VIEW research_signal_path_15m_enriched" in sql
    assert "MAX(p.favorable_return_pct) OVER w AS mfe_pct" in sql
    assert "MIN(p.adverse_return_pct) OVER w AS mae_pct" in sql
    # The migration intentionally does not bulk-copy historical path candles.
    assert "INSERT INTO research_signal_path_15m" not in sql
