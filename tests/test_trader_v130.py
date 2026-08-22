from dataclasses import replace
from datetime import UTC, datetime, timedelta

import pytest

from app.trader_config import TraderSettings
from app.trader_logic import tier_strategy_exit_reason
from tests.test_trader_v123 import FakeRepo, _position


def test_tier_exit_reason_standard_is_time_only():
    assert tier_strategy_exit_reason(
        exit_strategy="fixed_time_standard", position_maturity="7d",
        current_return_pct=80, age_seconds=6 * 86400, profit_target_pct=20,
    ) is None
    assert tier_strategy_exit_reason(
        exit_strategy="fixed_time_standard", position_maturity="7d",
        current_return_pct=-40, age_seconds=7 * 86400, profit_target_pct=20,
    ) == "standard_maturity_7d"


def test_tier_exit_reason_high_risk_is_tp20_or_4d():
    assert tier_strategy_exit_reason(
        exit_strategy="tp20_or_timeout", position_maturity="4d",
        current_return_pct=20, age_seconds=1, profit_target_pct=20,
    ) == "high_risk_profit_target_20"
    assert tier_strategy_exit_reason(
        exit_strategy="tp20_or_timeout", position_maturity="4d",
        current_return_pct=-30, age_seconds=4 * 86400, profit_target_pct=20,
    ) == "high_risk_timeout_4d"


@pytest.mark.asyncio
async def test_new_standard_position_holds_through_20_and_closes_at_7d(monkeypatch):
    import sys, types
    sys.modules.setdefault("asyncpg", types.SimpleNamespace())
    from app.trader import PortfolioShortTrader

    monkeypatch.setenv("DATABASE_URL", "postgresql://example")
    settings = TraderSettings.from_env()
    trader = PortfolioShortTrader(settings)
    p = _position(
        risk_tier="STANDARD", exit_strategy="fixed_time_standard", position_maturity="7d",
        opened_at=datetime.now(UTC) - timedelta(days=6),
    )
    repo = FakeRepo(p)
    trader.repo = repo
    closes = []
    notices = []

    async def fake_close(position, price, reason):
        closes.append((position, price, reason))

    async def fake_notify(title, description, event_fields=None, *, color=None):
        notices.append((title, description))

    monkeypatch.setattr(trader, "_close", fake_close)
    monkeypatch.setattr(trader, "_notify", fake_notify)

    await trader._monitor_position(repo.p, 0.70)  # +30%, but day 6 => hold
    assert closes == []
    assert repo.p.target_20_at is not None
    assert any("STANDARD +20%" in title for title, _ in notices)

    repo.p = replace(repo.p, opened_at=datetime.now(UTC) - timedelta(days=7, minutes=1))
    await trader._monitor_position(repo.p, 0.65)
    assert closes and closes[-1][2] == "standard_maturity_7d"


@pytest.mark.asyncio
async def test_new_high_risk_position_closes_at_20_or_4d(monkeypatch):
    import sys, types
    sys.modules.setdefault("asyncpg", types.SimpleNamespace())
    from app.trader import PortfolioShortTrader

    monkeypatch.setenv("DATABASE_URL", "postgresql://example")
    settings = TraderSettings.from_env()
    trader = PortfolioShortTrader(settings)
    closes = []

    async def fake_close(position, price, reason):
        closes.append(reason)

    async def fake_notify(*args, **kwargs):
        return None

    monkeypatch.setattr(trader, "_close", fake_close)
    monkeypatch.setattr(trader, "_notify", fake_notify)

    repo = FakeRepo(_position(
        risk_tier="HIGH_RISK", exit_strategy="tp20_or_timeout", position_maturity="4d",
        opened_at=datetime.now(UTC) - timedelta(hours=12),
    ))
    trader.repo = repo
    await trader._monitor_position(repo.p, 0.79)
    assert closes == ["high_risk_profit_target_20"]

    closes.clear()
    repo = FakeRepo(_position(
        risk_tier="HIGH_RISK", exit_strategy="tp20_or_timeout", position_maturity="4d",
        opened_at=datetime.now(UTC) - timedelta(days=4, minutes=1),
    ))
    trader.repo = repo
    await trader._monitor_position(repo.p, 1.25)
    assert closes == ["high_risk_timeout_4d"]


def test_default_capacity_is_exactly_five_standard_one_high(monkeypatch):
    monkeypatch.setenv("DATABASE_URL", "postgresql://example")
    monkeypatch.setenv("TRADER_EXECUTION_STRATEGY", "tier_v1")
    for key in (
        "TRADER_MAX_OPEN_POSITIONS", "TRADER_MAX_STANDARD_POSITIONS",
        "TRADER_MAX_HIGH_RISK_POSITIONS", "TRADER_STANDARD_HOLD_DAYS",
        "TRADER_HIGH_RISK_TIMEOUT_DAYS",
    ):
        monkeypatch.delenv(key, raising=False)
    settings = TraderSettings.from_env()
    assert settings.max_open_positions == 6
    assert settings.max_standard_positions == 5
    assert settings.max_high_risk_positions == 1
    assert settings.standard_hold_days == 7
    assert settings.high_risk_timeout_days == 4

@pytest.mark.asyncio
async def test_tier_capacity_reserves_five_standard_and_one_high(monkeypatch):
    import sys, types
    from types import SimpleNamespace
    sys.modules.setdefault("asyncpg", types.SimpleNamespace())
    from app.trader import PortfolioShortTrader
    from app.trader_models import TradeSignal

    monkeypatch.setenv("DATABASE_URL", "postgresql://example")
    monkeypatch.setenv("TRADER_EXECUTION_STRATEGY", "tier_v1")
    settings = TraderSettings.from_env()
    trader = PortfolioShortTrader(settings)
    decisions = []

    class Repo:
        def __init__(self, active):
            self.active = active
        async def active_positions(self):
            return self.active
        async def decision(self, signal_id, decision, reason, position_id=None):
            decisions.append((decision, reason))

    standards = [SimpleNamespace(symbol=f"S{i}_USDT", risk_tier="STANDARD", slot_no=i, notional_usdt=30.0) for i in range(1, 6)]
    trader.repo = Repo(standards)
    standard_signal = TradeSignal(
        id=1, symbol="NEWSTD_USDT", signaled_at=datetime.now(UTC), episode_id=1,
        entry_hint=1.0, risk_tier="STANDARD", features={},
    )
    await trader._handle_signal(standard_signal)
    assert decisions[-1][0] == "ignored_capacity"
    assert "STANDARD capacity" in decisions[-1][1]

    decisions.clear()
    one_high = [SimpleNamespace(symbol="H_USDT", risk_tier="HIGH_RISK", slot_no=6, notional_usdt=30.0)]
    trader.repo = Repo(one_high)
    high_signal = TradeSignal(
        id=2, symbol="NEWHIGH_USDT", signaled_at=datetime.now(UTC), episode_id=2,
        entry_hint=1.0, risk_tier="HIGH_RISK", features={},
    )
    await trader._handle_signal(high_signal)
    assert decisions[-1][0] == "ignored_capacity"
    assert "only one risky slot" in decisions[-1][1]
