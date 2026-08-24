from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace

import pytest

from app.db import Database
import app.research_analytics_now as research_now


@pytest.mark.asyncio
async def test_v138_path_sync_bounds_episode_set_and_keeps_candle_time_indexable():
    seen: dict[str, object] = {}

    class FakeTransaction:
        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            return False

    class FakeConn:
        def transaction(self):
            return FakeTransaction()

        async def execute(self, query: str, *args):
            seen["timeout"] = (query, args)

        async def fetchval(self, query: str, *args):
            seen["query"] = query
            seen["args"] = args
            return 12

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
        horizon_hours=336,
        statement_timeout_seconds=10,
    )

    assert inserted == 12
    query = str(seen["query"])
    assert "bounds AS MATERIALIZED" in query
    assert "LIMIT GREATEST(1, LEAST(32, CEIL($1::numeric / 256.0)::integer))" in query
    assert "AND c.open_time > b.last_recorded_close - interval '15 minutes'" in query
    assert "AND c.open_time <= now() - interval '15 minutes'" in query
    assert "ORDER BY c.open_time ASC" in query
    assert seen["args"] == (2000, 336)


@pytest.mark.asyncio
async def test_v138_on_demand_report_survives_optional_path_sync_timeout(monkeypatch, caplog):
    calls: list[str] = []

    settings = SimpleNamespace(
        database_url="postgresql://unused",
        discord_webhook_url=None,
        discord_signal_levels=set(),
        discord_performance_webhook_url="https://example.invalid/webhook",
        log_level="INFO",
        research_path_batch_rows=2000,
        research_path_horizon_hours=336,
        research_db_timeout_seconds=10,
        performance_report_timezone="Europe/Zurich",
    )

    class FakeSettings:
        @classmethod
        def from_env(cls):
            return settings

    class FakeDb:
        def __init__(self, database_url: str):
            assert database_url == settings.database_url

        async def connect(self):
            calls.append("connect")

        async def migrate(self):
            calls.append("migrate")

        async def sync_research_signal_snapshots(self):
            calls.append("snapshots")
            return 1

        async def sync_research_signal_paths(self, **kwargs):
            calls.append("paths")
            raise RuntimeError("canceling statement due to statement timeout")

        async def research_analytics_rows(self):
            calls.append("analytics_rows")
            return []

        async def research_delayed_entry_rows(self, **kwargs):
            return []

        async def research_portfolio_path_rows(self, **kwargs):
            return []

        async def research_regime_history_rows(self, **kwargs):
            return []

        async def close(self):
            calls.append("db_close")

    class FakeNotifier:
        def __init__(self, *args, **kwargs):
            pass

        async def send_research_analytics(self, *args, **kwargs):
            calls.append("send")
            return True

        async def close(self):
            calls.append("notifier_close")

    baseline = SimpleNamespace(
        total_signals=0,
        matured_7d=0,
        complete_paths_7d=0,
        complete_paths_14d=0,
        target_20_rate_7d=None,
        positive_7d_rate=None,
    )
    report = SimpleNamespace(baseline=baseline)

    monkeypatch.setattr(research_now, "Settings", FakeSettings)
    monkeypatch.setattr(research_now, "Database", FakeDb)
    monkeypatch.setattr(research_now, "DiscordNotifier", FakeNotifier)
    monkeypatch.setattr(research_now, "build_research_analytics", lambda *a, **k: report)
    monkeypatch.setattr(research_now, "research_signal_dataset_csv", lambda r: b"")

    caplog.set_level("ERROR")
    await research_now.main()

    assert calls[:4] == ["connect", "migrate", "snapshots", "paths"]
    assert "analytics_rows" in calls
    assert "send" in calls
    assert calls[-2:] == ["notifier_close", "db_close"]
    assert "Research path catch-up failed; continuing with currently persisted paths" in caplog.text
