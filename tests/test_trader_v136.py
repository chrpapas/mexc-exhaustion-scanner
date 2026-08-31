from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace

import pytest

from app.mexc_trade import MexcTradeClient, MexcTradeError
from app.trader_config import TraderSettings
from app.trader_models import TradeSignal
from tests.test_trader_v123 import FakeRepo, _position


def _signal(signal_id: int = 900, risk: str = "HIGH_RISK", symbol: str = "NEW_USDT") -> TradeSignal:
    return TradeSignal(
        id=signal_id,
        symbol=symbol,
        signaled_at=datetime.now(UTC),
        episode_id=signal_id,
        entry_hint=1.0,
        risk_tier=risk,
        features={},
    )


def test_tp5_sl75_pcr_v1_defaults_are_frozen(monkeypatch):
    monkeypatch.setenv("DATABASE_URL", "postgresql://example")
    for key in (
        "TRADER_EXECUTION_STRATEGY",
        "TRADER_PAPER_RUN_ID",
        "TRADER_MAX_OPEN_POSITIONS",
        "TRADER_SLOT_ALLOCATION_PCT",
        "TRADER_MAX_TOTAL_EXPOSURE_PCT",
        "TRADER_TP5_TARGET_PCT",
        "TRADER_CATASTROPHIC_STOP_PCT",
        "PAPER_STARTING_EQUITY_USDT",
    ):
        monkeypatch.delenv(key, raising=False)
    settings = TraderSettings.from_env()
    assert settings.execution_strategy == "tp5_sl75_pcr_v1"
    assert settings.paper_run_id == "tp5_sl75_pcr_v1"
    assert settings.max_open_positions == 6
    assert settings.slot_allocation_pct == pytest.approx(5.0)
    assert settings.max_total_exposure_pct == pytest.approx(30.0)
    assert settings.tp5_target_pct == pytest.approx(5.0)
    assert settings.catastrophic_stop_pct == pytest.approx(75.0)
    assert settings.uses_catastrophic_stop is True
    assert settings.paper_starting_equity_usdt == pytest.approx(2000.0)
    assert settings.uses_generic_slots is True


@pytest.mark.asyncio
async def test_tp5_position_closes_fully_at_five_percent_for_either_tier(monkeypatch):
    import sys, types
    sys.modules.setdefault("asyncpg", types.SimpleNamespace())
    from app.trader import PortfolioShortTrader

    monkeypatch.setenv("DATABASE_URL", "postgresql://example")
    settings = TraderSettings.from_env()
    trader = PortfolioShortTrader(settings)
    closes: list[str] = []

    async def fake_close(position, price, reason):
        closes.append(reason)

    monkeypatch.setattr(trader, "_close", fake_close)

    for risk in ("STANDARD", "HIGH_RISK"):
        repo = FakeRepo(
            _position(
                risk_tier=risk,
                exit_strategy="tp5_full",
                position_maturity="profit_5",
                metadata={"tp_target_pct": 5.0, "execution_strategy": "tp5_v1"},
            )
        )
        trader.repo = repo
        await trader._monitor_position(repo.p, 0.951)  # +4.9% -> keep open
        assert closes == []
        await trader._monitor_position(repo.p, 0.95)  # +5.0% -> full close
        assert closes == ["tp5_profit_target_5"]
        closes.clear()


@pytest.mark.asyncio
async def test_tp5_uses_generic_slots_and_ignores_legacy_tier_caps(monkeypatch):
    import sys, types
    sys.modules.setdefault("asyncpg", types.SimpleNamespace())
    from app.trader import PortfolioShortTrader

    monkeypatch.setenv("DATABASE_URL", "postgresql://example")
    settings = TraderSettings.from_env()
    trader = PortfolioShortTrader(settings)
    active = [
        SimpleNamespace(
            symbol=f"H{i}_USDT", risk_tier="HIGH_RISK", slot_no=i,
            notional_usdt=100.0, mode="paper",
        )
        for i in range(1, 6)
    ]
    decisions = []
    opened = []

    class Repo:
        async def active_positions(self):
            return active

        async def decision(self, signal_id, decision, reason, position_id=None):
            decisions.append((decision, reason))

    trader.repo = Repo()

    async def fake_equity(_active=None):
        return 2000.0

    async def fake_open(signal, slot_no, equity, notional):
        opened.append((signal.risk_tier, slot_no, equity, notional))
        return SimpleNamespace(
            id=999, symbol=signal.symbol, risk_tier=signal.risk_tier, slot_no=slot_no,
            notional_usdt=notional, mode="paper", entry_price=1.0,
        )

    async def fake_notify(*args, **kwargs):
        return None

    monkeypatch.setattr(trader, "_account_equity", fake_equity)
    monkeypatch.setattr(trader, "_open_paper", fake_open)
    monkeypatch.setattr(trader, "_notify", fake_notify)

    await trader._handle_signal(_signal(risk="HIGH_RISK"))
    assert opened == [("HIGH_RISK", 6, 2000.0, 100.0)]
    assert decisions == []


@pytest.mark.asyncio
async def test_paper_run_reset_is_one_time_per_run_id(monkeypatch):
    import sys, types
    sys.modules.setdefault("asyncpg", types.SimpleNamespace())
    from app.trader import PortfolioShortTrader

    monkeypatch.setenv("DATABASE_URL", "postgresql://example")
    monkeypatch.setenv("TRADER_PAPER_RUN_ID", "tp5_v1_test")
    monkeypatch.setenv("PAPER_STARTING_EQUITY_USDT", "2000")
    settings = TraderSettings.from_env()
    trader = PortfolioShortTrader(settings)
    old = _position(mode="paper", current_price=1.1, run_id="legacy_pre_v136")
    state = {"run": "legacy_pre_v136"}
    archived = []
    activations = []

    class Repo:
        async def runtime(self):
            return {"active_run_id": state["run"], "paper_equity_usdt": 2250.0, "last_signal_id": 1}

        async def run_record(self, run_id):
            return None

        async def active_positions(self):
            return [old] if state["run"] == "legacy_pre_v136" else []

        async def close_position(self, position, **kwargs):
            archived.append((position.id, kwargs["reason"], kwargs["exit_price"]))
            return position

        async def activate_paper_run(self, **kwargs):
            activations.append(kwargs)
            state["run"] = kwargs["run_id"]
            return True

    class Ticker:
        async def remove(self, symbol):
            return None

    class Mexc:
        ticker_stream = Ticker()

        async def last_price(self, symbol):
            return 1.2

    trader.repo = Repo()
    trader.mexc = Mexc()

    await trader._ensure_paper_run()
    assert archived and archived[0][1] == "strategy_run_reset_to_tp5_v1_test"
    assert activations[0]["starting_equity"] == pytest.approx(2000.0)
    assert activations[0]["run_id"] == "tp5_v1_test"

    # Same run ID on a normal redeploy must resume, not reset again.
    await trader._ensure_paper_run()
    assert len(archived) == 1
    assert len(activations) == 1


@pytest.mark.asyncio
async def test_mexc_available_balance_uses_explicit_futures_available_balance(monkeypatch):
    client = MexcTradeClient("https://api.mexc.com")

    async def asset():
        return {"equity": 1234.0, "cashBalance": 1200.0, "availableBalance": 987.65}

    monkeypatch.setattr(client, "usdt_asset", asset)
    try:
        assert await client.usdt_available_balance() == pytest.approx(987.65)
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_mexc_available_balance_fails_closed_if_field_is_missing(monkeypatch):
    client = MexcTradeClient("https://api.mexc.com")

    async def asset():
        return {"equity": 1234.0, "cashBalance": 1200.0}

    monkeypatch.setattr(client, "usdt_asset", asset)
    try:
        # cashBalance is drawable balance, not necessarily currently available margin.
        with pytest.raises(MexcTradeError, match="available-balance"):
            await client.usdt_available_balance()
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_tp5_live_does_not_open_partial_slot_when_available_cash_is_insufficient(monkeypatch):
    import sys, types
    sys.modules.setdefault("asyncpg", types.SimpleNamespace())
    from app.trader import PortfolioShortTrader

    monkeypatch.setenv("DATABASE_URL", "postgresql://example")
    monkeypatch.setenv("TRADING_MODE", "live")
    monkeypatch.setenv("MEXC_API_KEY", "key")
    monkeypatch.setenv("MEXC_API_SECRET", "secret")
    monkeypatch.setenv("MEXC_LIVE_ORDER_API_ENABLED", "true")
    monkeypatch.setenv("LIVE_TRADING_CONFIRM", "I_UNDERSTAND_LIVE_TRADING")
    settings = TraderSettings.from_env()
    trader = PortfolioShortTrader(settings)
    decisions = []
    opened = []

    class Repo:
        async def active_positions(self):
            return []

        async def decision(self, signal_id, decision, reason, position_id=None):
            decisions.append((decision, reason))

    class Mexc:
        async def usdt_available_balance(self):
            return 90.0  # 5% of 2,000 is 100; after buffer this is not a full slot.

    trader.repo = Repo()
    trader.mexc = Mexc()

    async def fake_equity(_active=None):
        return 2000.0

    async def fake_open(*args, **kwargs):
        opened.append((args, kwargs))

    monkeypatch.setattr(trader, "_account_equity", fake_equity)
    monkeypatch.setattr(trader, "_open_live", fake_open)

    await trader._handle_signal(_signal())
    assert opened == []
    assert decisions[-1][0] == "ignored_exposure"

@pytest.mark.asyncio
async def test_archived_paper_run_id_cannot_be_reused():
    from app.trader_db import TraderRepository

    class Pool:
        def __init__(self):
            self.calls = 0

        async def fetchrow(self, query, *args):
            self.calls += 1
            if "FROM trader_runtime" in query:
                return {
                    "last_signal_id": 10,
                    "paper_equity_usdt": 2100.0,
                    "active_run_id": "tp5_v2",
                    "initialized_at": datetime.now(UTC),
                    "updated_at": datetime.now(UTC),
                }
            if "FROM trader_runs" in query:
                return {"run_id": "tp5_v1", "status": "archived"}
            raise AssertionError(query)

    repo = TraderRepository(SimpleNamespace(pool=Pool()))
    with pytest.raises(RuntimeError, match="already used"):
        await repo.activate_paper_run(
            run_id="tp5_v1",
            starting_equity=2000.0,
            process_existing=False,
            strategy_name="tp5_v1",
        )

@pytest.mark.asyncio
async def test_reused_paper_run_id_is_rejected_before_current_book_is_touched(monkeypatch):
    import sys, types
    sys.modules.setdefault("asyncpg", types.SimpleNamespace())
    from app.trader import PortfolioShortTrader

    monkeypatch.setenv("DATABASE_URL", "postgresql://example")
    monkeypatch.setenv("TRADER_PAPER_RUN_ID", "old_archived_run")
    settings = TraderSettings.from_env()
    trader = PortfolioShortTrader(settings)
    touched = []

    class Repo:
        async def runtime(self):
            return {"active_run_id": "current_run", "paper_equity_usdt": 2100.0, "last_signal_id": 1}

        async def run_record(self, run_id):
            return {"run_id": run_id, "status": "archived"}

        async def active_positions(self):
            touched.append("listed")
            return [_position(mode="paper", run_id="current_run")]

        async def close_position(self, *args, **kwargs):
            touched.append("closed")

    trader.repo = Repo()
    with pytest.raises(RuntimeError, match="already used"):
        await trader._ensure_paper_run()
    assert touched == []
