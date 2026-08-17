from dataclasses import replace
from datetime import UTC, datetime

import pytest

from app.trader_config import TraderSettings
from app.trader_models import TraderPosition


def _position(**overrides):
    base = dict(
        id=1,
        signal_id=10,
        symbol="TEST_USDT",
        risk_tier="HIGH_RISK",
        slot_no=1,
        mode="paper",
        capital_strategy="portfolio_cross_1x",
        exit_strategy="profit_runner",
        position_maturity="runner",
        status="open",
        opened_at=datetime.now(UTC),
        entry_price=1.0,
        entry_equity_usdt=1000.0,
        notional_usdt=33.3333,
        quantity_base=33.3333,
        current_price=1.0,
        current_return_pct=0.0,
        peak_profit_pct=0.0,
        max_adverse_pct=0.0,
        profit_floor_pct=None,
        liquidation_proxy_pct=400.0,
        target_20_at=None,
        protection_armed_at=None,
        mexc_protection_order_id=None,
        breach_100_at=None,
        breach_200_at=None,
        breach_300_at=None,
        breach_400_at=None,
        entry_fee_usdt=0.0,
        exit_fee_usdt=0.0,
        mexc_position_id=None,
        mexc_open_order_id=None,
        metadata={},
    )
    base.update(overrides)
    return TraderPosition(**base)


class FakeRepo:
    def __init__(self, position):
        self.p = position

    async def update_market(self, position_id, *, price, return_pct, peak_profit_pct, max_adverse_pct, profit_floor_pct):
        self.p = replace(
            self.p,
            current_price=price,
            current_return_pct=return_pct,
            peak_profit_pct=peak_profit_pct,
            max_adverse_pct=max_adverse_pct,
            profit_floor_pct=profit_floor_pct,
        )
        return self.p

    async def mark_breach(self, position_id, threshold, *, price, return_pct):
        field = {100: "breach_100_at", 200: "breach_200_at", 300: "breach_300_at", 400: "breach_400_at"}[threshold]
        if getattr(self.p, field) is not None:
            return False
        self.p = replace(self.p, **{field: datetime.now(UTC)})
        return True

    async def mark_target_hit(self, position_id, *, price, return_pct):
        if self.p.target_20_at is not None:
            return False
        self.p = replace(self.p, target_20_at=datetime.now(UTC))
        return True

    async def position(self, position_id):
        return self.p


@pytest.mark.asyncio
async def test_target_20_milestone_is_discord_notified_once(monkeypatch):
    import sys, types
    sys.modules.setdefault("asyncpg", types.SimpleNamespace())
    from app.trader import PortfolioShortTrader

    monkeypatch.setenv("DATABASE_URL", "postgresql://example")
    settings = TraderSettings.from_env()
    trader = PortfolioShortTrader(settings)
    repo = FakeRepo(_position())
    trader.repo = repo
    notices = []

    async def fake_notify(title, description, event_fields=None, *, color=None):
        notices.append((title, description, event_fields, color))

    monkeypatch.setattr(trader, "_notify", fake_notify)

    await trader._monitor_position(repo.p, 0.79)  # +21%
    await trader._monitor_position(repo.p, 0.78)  # +22%, same milestone must not repeat

    target_notices = [n for n in notices if "+20% PROFIT MILESTONE" in n[0]]
    assert len(target_notices) == 1
    assert "runner stays open" in target_notices[0][1]
    assert any(f["name"] == "Current return" for f in target_notices[0][2])


@pytest.mark.asyncio
async def test_each_adverse_threshold_is_discord_notified_once_and_cumulatively(monkeypatch):
    import sys, types
    sys.modules.setdefault("asyncpg", types.SimpleNamespace())
    from app.trader import PortfolioShortTrader

    monkeypatch.setenv("DATABASE_URL", "postgresql://example")
    settings = TraderSettings.from_env()
    trader = PortfolioShortTrader(settings)
    repo = FakeRepo(_position())
    trader.repo = repo
    notices = []

    async def fake_notify(title, description, event_fields=None, *, color=None):
        notices.append((title, description, event_fields, color))

    monkeypatch.setattr(trader, "_notify", fake_notify)

    await trader._monitor_position(repo.p, 3.5)  # -250% => crosses -100 and -200
    await trader._monitor_position(repo.p, 5.2)  # -420% => adds -300 and -400
    await trader._monitor_position(repo.p, 5.3)  # no duplicate alerts

    breach_titles = [n[0] for n in notices if "ADVERSE BREACH" in n[0]]
    assert len(breach_titles) == 4
    assert sum("-100%" in title for title in breach_titles) == 1
    assert sum("-200%" in title for title in breach_titles) == 1
    assert sum("-300%" in title for title in breach_titles) == 1
    assert sum("-400%" in title for title in breach_titles) == 1
    assert repo.p.breach_100_at is not None
    assert repo.p.breach_200_at is not None
    assert repo.p.breach_300_at is not None
    assert repo.p.breach_400_at is not None
