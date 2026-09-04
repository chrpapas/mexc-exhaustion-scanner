from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace

import pytest

from app.daily_bull_persistence_strategy import (
    DAILY_CORE_PERSISTENCE_SKIP_STRATEGY,
    daily_bull_persistence_v1_missing_features,
    daily_bull_persistence_v1_state,
)
from app.models import RunSignal
from app.trader_config import TraderSettings
from app.trader_models import TradeSignal


def _features(*, persistence: bool = True, core: bool = False, daily_bull: bool = True) -> dict[str, object]:
    return {
        "risk_tier": "standard",
        "run_score": 5 if core else 4,
        "exhaustion_score": 4,
        "distance_above_ema20_atr_4h": 3.5,
        "previous_momentum_1h": 0.02,
        "cross_section_percentile": 0.995,
        "daily_close_above_ema20": daily_bull,
        "daily_ema20_slope": 0.08 if daily_bull else -0.01,
        "daily_momentum_3d": 0.50 if daily_bull else -0.02,
        "daily_distance_above_ema20_atr": 5.0 if persistence else 4.0,
        "hours_run_to_breakdown": 5.0,
        "retest_close": 1.0,
    }


def test_promoted_persistence_classifier_is_selective_and_tri_state():
    assert daily_bull_persistence_v1_state(_features()) is True
    assert daily_bull_persistence_v1_state(_features(persistence=False)) is False
    assert daily_bull_persistence_v1_state(_features(core=True)) is False
    assert daily_bull_persistence_v1_state(_features(daily_bull=False)) is False

    missing = _features()
    missing.pop("hours_run_to_breakdown")
    assert daily_bull_persistence_v1_state(missing) is None
    assert daily_bull_persistence_v1_missing_features(missing) == ("hours_run_to_breakdown",)


def test_persistence_strategy_is_default_without_resetting_existing_paper_run(monkeypatch):
    monkeypatch.setenv("DATABASE_URL", "postgresql://example")
    monkeypatch.delenv("TRADER_EXECUTION_STRATEGY", raising=False)
    monkeypatch.delenv("TRADER_PAPER_RUN_ID", raising=False)
    settings = TraderSettings.from_env()
    assert settings.execution_strategy == DAILY_CORE_PERSISTENCE_SKIP_STRATEGY
    assert settings.uses_daily_core_skip
    assert settings.uses_daily_bull_persistence_skip
    # Intentionally retained so promotion applies to future entries without closing the current paper book.
    assert settings.paper_run_id == "tp5_sl75_daily_core_skip_v1"
    assert settings.slot_allocation_pct == pytest.approx(5.0)
    assert settings.max_total_exposure_pct == pytest.approx(30.0)


@pytest.mark.asyncio
async def test_promoted_trader_skips_persistence_flag_and_missing_but_opens_safe(monkeypatch):
    import sys, types
    sys.modules.setdefault("asyncpg", types.SimpleNamespace())
    from app.trader import PortfolioShortTrader

    monkeypatch.setenv("DATABASE_URL", "postgresql://example")
    monkeypatch.setenv("TRADER_EXECUTION_STRATEGY", DAILY_CORE_PERSISTENCE_SKIP_STRATEGY)
    settings = TraderSettings.from_env()
    trader = PortfolioShortTrader(settings)
    decisions: list[tuple[int, str]] = []
    opened: list[tuple[int, float]] = []

    class Repo:
        async def active_positions(self):
            return []

        async def decision(self, signal_id, decision, reason, position_id=None):
            decisions.append((signal_id, decision))

    trader.repo = Repo()

    async def equity(_active=None):
        return 2000.0

    async def open_paper(signal, slot_no, equity_value, notional):
        opened.append((signal.id, notional))
        return SimpleNamespace(
            id=signal.id,
            symbol=signal.symbol,
            risk_tier=signal.risk_tier,
            slot_no=slot_no,
            notional_usdt=notional,
            mode="paper",
            entry_price=1.0,
        )

    async def quiet(*args, **kwargs):
        return None

    monkeypatch.setattr(trader, "_account_equity", equity)
    monkeypatch.setattr(trader, "_open_paper", open_paper)
    monkeypatch.setattr(trader, "_notify", quiet)

    def signal(signal_id: int, features: dict[str, object]) -> TradeSignal:
        return TradeSignal(
            id=signal_id,
            symbol=f"S{signal_id}_USDT",
            signaled_at=datetime.now(UTC),
            episode_id=signal_id,
            entry_hint=1.0,
            risk_tier="STANDARD",
            features=features,
        )

    await trader._handle_signal(signal(1, _features()))
    assert decisions[-1] == (1, "ignored_daily_bull_persistence_filter")
    assert opened == []

    missing = _features()
    missing.pop("hours_run_to_breakdown")
    await trader._handle_signal(signal(2, missing))
    assert decisions[-1] == (2, "ignored_missing_persistence_data")
    assert opened == []

    await trader._handle_signal(signal(3, _features(persistence=False)))
    assert opened == [(3, pytest.approx(100.0))]


@pytest.mark.asyncio
async def test_plain_daily_core_rollback_does_not_apply_persistence_veto(monkeypatch):
    import sys, types
    sys.modules.setdefault("asyncpg", types.SimpleNamespace())
    from app.trader import PortfolioShortTrader

    monkeypatch.setenv("DATABASE_URL", "postgresql://example")
    monkeypatch.setenv("TRADER_EXECUTION_STRATEGY", "tp5_sl75_daily_core_skip_v1")
    settings = TraderSettings.from_env()
    assert settings.uses_daily_core_skip
    assert not settings.uses_daily_bull_persistence_skip

    trader = PortfolioShortTrader(settings)
    opened: list[int] = []

    class Repo:
        async def active_positions(self):
            return []
        async def decision(self, *args, **kwargs):
            return None
    trader.repo = Repo()

    async def equity(_active=None): return 2000.0
    async def open_paper(signal, slot_no, equity_value, notional):
        opened.append(signal.id)
        return SimpleNamespace(id=signal.id, symbol=signal.symbol, risk_tier=signal.risk_tier, slot_no=slot_no, notional_usdt=notional, mode="paper", entry_price=1.0)
    async def quiet(*args, **kwargs): return None
    monkeypatch.setattr(trader, "_account_equity", equity)
    monkeypatch.setattr(trader, "_open_paper", open_paper)
    monkeypatch.setattr(trader, "_notify", quiet)

    sig = TradeSignal(9, "ROLLBACK_USDT", datetime.now(UTC), 9, 1.0, "STANDARD", _features())
    await trader._handle_signal(sig)
    assert opened == [9]


@pytest.mark.asyncio
async def test_promoted_subscriber_filter_matches_trader():
    from app.notifier import DiscordNotifier

    class Response:
        def raise_for_status(self): return None
    class Client:
        def __init__(self): self.posts = []
        async def post(self, url, **kwargs):
            self.posts.append((url, kwargs)); return Response()

    notifier = DiscordNotifier(
        "https://discord.invalid/signals",
        subscriber_signal_strategy=DAILY_CORE_PERSISTENCE_SKIP_STRATEGY,
    )
    fake = Client(); notifier._client = fake

    def signal(symbol: str, features: dict[str, object]) -> RunSignal:
        return RunSignal(symbol, datetime.now(UTC), "confirmed_short", 8, features, ["test"], 1)

    await notifier.send_signal(signal("FLAG_USDT", _features()))
    assert fake.posts == []
    await notifier.send_signal(signal("SAFE_USDT", _features(persistence=False)))
    assert len(fake.posts) == 1


def test_migration_018_allows_distinct_persistence_decisions():
    sql = Path("migrations/018_daily_bull_persistence_trader_decisions.sql").read_text()
    assert "ignored_daily_bull_persistence_filter" in sql
    assert "ignored_missing_persistence_data" in sql
    assert "ignored_daily_core_filter" in sql
