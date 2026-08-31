from __future__ import annotations

from types import SimpleNamespace

import pytest

from app.trader_config import TraderSettings
from tests.test_trader_v123 import FakeRepo, _position


def test_pcr_sl75_default_configuration(monkeypatch):
    monkeypatch.setenv("DATABASE_URL", "postgresql://example")
    for key in (
        "TRADER_EXECUTION_STRATEGY", "TRADER_PAPER_RUN_ID", "TRADER_MAX_OPEN_POSITIONS",
        "TRADER_SLOT_ALLOCATION_PCT", "TRADER_MAX_TOTAL_EXPOSURE_PCT",
        "TRADER_TP5_TARGET_PCT", "TRADER_CATASTROPHIC_STOP_PCT",
    ):
        monkeypatch.delenv(key, raising=False)
    settings = TraderSettings.from_env()
    assert settings.execution_strategy == "tp5_sl75_pcr_v1"
    assert settings.paper_run_id == "tp5_sl75_pcr_v1"
    assert settings.uses_generic_slots is True
    assert settings.uses_catastrophic_stop is True
    assert settings.max_open_positions == 6
    assert settings.slot_allocation_pct == pytest.approx(5.0)
    assert settings.max_total_exposure_pct == pytest.approx(30.0)
    assert settings.tp5_target_pct == pytest.approx(5.0)
    assert settings.catastrophic_stop_pct == pytest.approx(75.0)


@pytest.mark.asyncio
async def test_sl75_position_closes_at_catastrophic_stop_before_tp(monkeypatch):
    import sys, types
    sys.modules.setdefault("asyncpg", types.SimpleNamespace())
    from app.trader import PortfolioShortTrader

    monkeypatch.setenv("DATABASE_URL", "postgresql://example")
    settings = TraderSettings.from_env()
    trader = PortfolioShortTrader(settings)
    repo = FakeRepo(
        _position(
            exit_strategy="tp5_sl75_full",
            position_maturity="profit_5",
            metadata={
                "tp_target_pct": 5.0,
                "catastrophic_stop_pct": 75.0,
                "execution_strategy": "tp5_sl75_v1",
            },
        )
    )
    trader.repo = repo
    closes: list[str] = []

    async def fake_close(position, price, reason):
        closes.append(reason)

    monkeypatch.setattr(trader, "_close", fake_close)
    await trader._monitor_position(repo.p, 1.749)  # -74.9%
    assert closes == []
    await trader._monitor_position(repo.p, 1.75)   # -75.0%
    assert closes == ["tp5_catastrophic_stop_75"]


@pytest.mark.asyncio
async def test_legacy_tp5_full_remains_no_stop(monkeypatch):
    import sys, types
    sys.modules.setdefault("asyncpg", types.SimpleNamespace())
    from app.trader import PortfolioShortTrader

    monkeypatch.setenv("DATABASE_URL", "postgresql://example")
    settings = TraderSettings.from_env()
    trader = PortfolioShortTrader(settings)
    repo = FakeRepo(
        _position(
            exit_strategy="tp5_full",
            position_maturity="profit_5",
            metadata={"tp_target_pct": 5.0, "execution_strategy": "tp5_v1"},
        )
    )
    trader.repo = repo
    closes: list[str] = []

    async def fake_close(position, price, reason):
        closes.append(reason)

    monkeypatch.setattr(trader, "_close", fake_close)
    await trader._monitor_position(repo.p, 2.0)  # -100%; old persisted position stays legacy
    assert closes == []
