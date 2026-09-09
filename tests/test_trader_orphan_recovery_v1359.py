from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace

import pytest

from app.trader_db import TraderRepository


def _row(signal_id: int = 819):
    return {
        "id": signal_id,
        "symbol": "ETC_USDT",
        "signaled_at": datetime.now(UTC),
        "episode_id": 819,
        "features": {"risk_tier": "STANDARD", "retest_close": 8.474},
        "episode_started_at": datetime.now(UTC),
        "episode_breakdown_at": datetime.now(UTC),
    }


@pytest.mark.asyncio
async def test_recent_unprocessed_query_requires_no_decision_and_no_position():
    calls = []

    class Pool:
        async def fetch(self, query, *args):
            calls.append((query, args))
            return [_row()]

    repo = TraderRepository(SimpleNamespace(pool=Pool()))
    signals = await repo.recent_unprocessed_confirmed_signals(max_age_seconds=900)
    assert len(signals) == 1
    assert signals[0].symbol == "ETC_USDT"
    query, args = calls[0]
    assert "trader_signal_decisions" in query
    assert "trader_positions" in query
    assert "td.signal_id IS NULL" in query
    assert "tp.signal_id IS NULL" in query
    assert args[0] == 900.0


def test_trade_signal_row_conversion_preserves_armed_runner_signal():
    signals = TraderRepository._trade_signals_from_rows([_row()])
    signal = signals[0]
    assert signal.id == 819
    assert signal.risk_tier == "STANDARD"
    assert signal.entry_hint == pytest.approx(8.474)
    assert "hours_run_to_breakdown" in signal.features
