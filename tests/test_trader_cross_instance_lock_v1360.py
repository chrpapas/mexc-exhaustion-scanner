from __future__ import annotations

from types import SimpleNamespace

import pytest

from app.trader_db import TraderRepository


class _Conn:
    def __init__(self, locked: bool) -> None:
        self.locked = locked
        self.calls: list[tuple[str, tuple[object, ...]]] = []

    async def fetchval(self, query: str, *args):
        self.calls.append((query, args))
        if "pg_try_advisory_lock" in query:
            return self.locked
        if "pg_advisory_unlock" in query:
            return True
        raise AssertionError(query)


class _Acquire:
    def __init__(self, conn: _Conn) -> None:
        self.conn = conn

    async def __aenter__(self):
        return self.conn

    async def __aexit__(self, exc_type, exc, tb):
        return False


class _Pool:
    def __init__(self, conn: _Conn) -> None:
        self.conn = conn

    def acquire(self):
        return _Acquire(self.conn)


@pytest.mark.asyncio
async def test_signal_consumer_lock_acquires_and_releases():
    conn = _Conn(True)
    repo = TraderRepository(SimpleNamespace(pool=_Pool(conn)))
    async with repo.signal_consumer_lock() as acquired:
        assert acquired is True
    assert any("pg_try_advisory_lock" in query for query, _ in conn.calls)
    assert any("pg_advisory_unlock" in query for query, _ in conn.calls)


@pytest.mark.asyncio
async def test_signal_consumer_lock_loser_does_not_unlock():
    conn = _Conn(False)
    repo = TraderRepository(SimpleNamespace(pool=_Pool(conn)))
    async with repo.signal_consumer_lock() as acquired:
        assert acquired is False
    assert any("pg_try_advisory_lock" in query for query, _ in conn.calls)
    assert not any("pg_advisory_unlock" in query for query, _ in conn.calls)


def test_strategy_label_uses_configured_833_percent(monkeypatch):
    import sys
    import types

    sys.modules.setdefault("asyncpg", types.SimpleNamespace())
    from app.trader import PortfolioShortTrader
    from app.trader_config import TraderSettings

    monkeypatch.setenv("DATABASE_URL", "postgresql://example")
    settings = TraderSettings.from_env()
    trader = PortfolioShortTrader(settings)
    label = trader._strategy_label()
    assert "6 generic slots × 8.33%" in label
    assert "max 50.0% exposure" in label
