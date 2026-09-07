from __future__ import annotations

import inspect
from pathlib import Path

import pytest

from app.db import Database


@pytest.mark.asyncio
async def test_v153_path_sync_uses_set_based_progress_not_per_trade_lateral_aggregate():
    seen: dict[str, object] = {}

    class Tx:
        async def __aenter__(self): return self
        async def __aexit__(self, exc_type, exc, tb): return False

    class Conn:
        def transaction(self): return Tx()
        async def execute(self, query: str, *args): seen['timeout'] = (query, args)
        async def fetchval(self, query: str, *args):
            seen['query'] = query
            seen['args'] = args
            return 7

    class Acquire:
        async def __aenter__(self): return Conn()
        async def __aexit__(self, exc_type, exc, tb): return False

    class Pool:
        def acquire(self): return Acquire()

    db = Database('postgresql://unused')
    db._pool = Pool()
    inserted = await db.sync_research_signal_paths(
        batch_rows=2000,
        horizon_hours=336,
        statement_timeout_seconds=10,
    )

    assert inserted == 7
    query = str(seen['query'])
    assert 'last_progress AS MATERIALIZED' in query
    assert 'target_progress AS MATERIALIZED' in query
    assert 'SELECT DISTINCT ON (episode_id)' in query
    assert 'WHERE favorable_return_pct >= 0.05' in query
    assert 'LEFT JOIN last_progress lp ON lp.episode_id = st.episode_id' in query
    assert 'LEFT JOIN target_progress tp ON tp.episode_id = st.episode_id' in query
    assert 'LEFT JOIN LATERAL (\n                    SELECT\n                        max(candle_close_at)' not in query
    assert seen['args'] == (2000, 336)
    assert seen['timeout'][1] == ('10s',)


def test_v153_migration_adds_progress_indexes():
    migration = Path('migrations/019_research_path_progress_indexes.sql').read_text()
    assert 'ix_research_signal_path_episode_time_desc' in migration
    assert 'ON research_signal_path_15m(episode_id, candle_close_at DESC)' in migration
    assert 'ix_research_signal_path_target5_episode_time' in migration
    assert 'WHERE favorable_return_pct >= 0.05' in migration


def test_v153_does_not_change_promoted_strategy_identity():
    from app.daily_bull_persistence_strategy import (
        DAILY_BULL_PERSISTENCE_V1_VERSION,
        DAILY_CORE_PERSISTENCE_SKIP_STRATEGY,
    )

    assert DAILY_BULL_PERSISTENCE_V1_VERSION == 'daily_bull_persistence_v1'
    assert DAILY_CORE_PERSISTENCE_SKIP_STRATEGY == 'tp5_sl75_daily_core_persistence_skip_v1'
