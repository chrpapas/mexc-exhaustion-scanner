from __future__ import annotations

from datetime import UTC, datetime

import pytest

from app.db import Database


@pytest.mark.asyncio
async def test_v154_performance_rows_batches_path_fetches_by_episode():
    confirmed = datetime(2026, 9, 1, tzinfo=UTC)
    base_rows = [
        {
            "episode_id": episode_id,
            "symbol": f"T{episode_id}_USDT",
            "confirmed_at": confirmed,
            "entry_price": 1.0,
            "risk_tier": "standard",
            "current_return_pct": 0.0,
            "mfe_pct": 0.0,
            "mae_pct": 0.0,
            "return_1h_pct": None,
            "return_4h_pct": None,
            "return_12h_pct": None,
            "return_24h_pct": None,
            "return_48h_pct": None,
            "return_72h_pct": None,
            "return_168h_pct": None,
            "matured_at": None,
            "matured_48h_at": None,
            "matured_72h_at": None,
            "matured_168h_at": None,
            "first_profit_at": None,
            "target_20_at": None,
            "isolated_100_breach_at": None,
            "adverse_200_breach_at": None,
            "adverse_300_breach_at": None,
            "cross_400_breach_at": None,
            "feature_snapshot": {},
        }
        for episode_id in range(1, 34)
    ]
    calls: list[tuple[str, tuple[object, ...]]] = []

    class Pool:
        async def fetch(self, query: str, *args):
            calls.append((query, args))
            if "FROM shadow_trades st" in query:
                return base_rows
            episode_ids = args[0]
            assert len(episode_ids) <= 16
            return []

    db = Database("postgresql://unused")
    db._pool = Pool()
    rows = await db.performance_rows()

    assert len(rows) == 33
    path_calls = [call for call in calls if "FROM research_signal_path_15m" in call[0]]
    assert len(path_calls) == 3
    assert [len(call[1][0]) for call in path_calls] == [16, 16, 1]
    assert rows[0]["target_5_at"] is None
    assert rows[-1]["path_times"] is None


def test_v154_performance_rows_no_longer_has_monolithic_all_episode_path_fetch():
    source = open("app/db.py", encoding="utf-8").read()
    assert "path_episode_batch_size = 16" in source
    assert "batch_episode_ids = episode_ids[offset : offset + path_episode_batch_size]" in source
    assert "WHERE episode_id = ANY($1::bigint[])" in source
