from __future__ import annotations

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest

from app.daily_core_strategy import (
    daily_confirmed_core_v1_state,
    daily_confirmed_core_v1_snapshot_metadata,
)
from app.models import Candle, RunSignal
from app.trader_config import TraderSettings
from app.trader_models import TradeSignal


def _features(*, daily_bull: bool = True, core: bool = True) -> dict[str, object]:
    return {
        "run_score": 5 if core else 4,
        "distance_above_ema20_atr_4h": 3.5,
        "previous_momentum_1h": 0.02,
        "cross_section_percentile": 0.995,
        "daily_close_above_ema20": daily_bull,
        "daily_ema20_slope": 0.01 if daily_bull else -0.01,
        "daily_momentum_3d": 0.05 if daily_bull else -0.02,
    }


def test_daily_core_classifier_is_shared_tri_state():
    flagged = _features()
    assert daily_confirmed_core_v1_state(flagged) is True
    assert daily_confirmed_core_v1_state(_features(core=False)) is False
    assert daily_confirmed_core_v1_state(_features(daily_bull=False)) is False
    missing = dict(flagged)
    missing.pop("daily_momentum_3d")
    assert daily_confirmed_core_v1_state(missing) is None
    metadata = daily_confirmed_core_v1_snapshot_metadata(flagged)
    assert metadata["daily_confirmed_core_v1_computable"] is True
    assert metadata["daily_confirmed_core_v1_flagged"] is True


def test_daily_core_skip_is_default_and_pcr_is_rollback(monkeypatch):
    monkeypatch.setenv("DATABASE_URL", "postgresql://example")
    monkeypatch.delenv("TRADER_EXECUTION_STRATEGY", raising=False)
    monkeypatch.delenv("TRADER_PAPER_RUN_ID", raising=False)
    default = TraderSettings.from_env()
    assert default.execution_strategy == "tp5_sl75_daily_core_skip_v1"
    assert default.paper_run_id == "tp5_sl75_daily_core_skip_v1"
    assert default.uses_daily_core_skip
    assert default.uses_catastrophic_stop
    assert default.slot_allocation_pct == pytest.approx(5.0)
    assert default.max_total_exposure_pct == pytest.approx(30.0)

    monkeypatch.setenv("TRADER_EXECUTION_STRATEGY", "tp5_sl75_pcr_v1")
    rollback = TraderSettings.from_env()
    assert rollback.uses_pcr_derisk
    assert not rollback.uses_daily_core_skip


@pytest.mark.asyncio
async def test_subscriber_notifier_hard_filters_flagged_and_missing_but_sends_safe():
    from app.notifier import DiscordNotifier

    class Response:
        def raise_for_status(self):
            return None

    class Client:
        def __init__(self):
            self.posts = []

        async def post(self, url, **kwargs):
            self.posts.append((url, kwargs))
            return Response()

    notifier = DiscordNotifier(
        "https://discord.invalid/signals",
        subscriber_signal_strategy="tp5_sl75_daily_core_skip_v1",
    )
    fake = Client()
    notifier._client = fake

    def run_signal(symbol: str, features: dict[str, object]) -> RunSignal:
        data = dict(features)
        data.update({"risk_tier": "standard", "run_score": data.get("run_score", 5), "exhaustion_score": 4})
        return RunSignal(
            symbol=symbol,
            signaled_at=datetime(2026, 9, 2, 6, 0, tzinfo=UTC),
            level="confirmed_short",
            score=9,
            features=data,
            reasons=["test"],
            episode_id=1,
        )

    await notifier.send_signal(run_signal("FLAG_USDT", _features()))
    missing = _features()
    missing.pop("daily_momentum_3d")
    await notifier.send_signal(run_signal("MISS_USDT", missing))
    assert fake.posts == []

    await notifier.send_signal(run_signal("SAFE_USDT", _features(core=False)))
    assert len(fake.posts) == 1


@pytest.mark.asyncio
async def test_trader_hard_skips_flagged_fails_closed_and_opens_safe(monkeypatch):
    import sys, types
    sys.modules.setdefault("asyncpg", types.SimpleNamespace())
    from app.trader import PortfolioShortTrader

    monkeypatch.setenv("DATABASE_URL", "postgresql://example")
    monkeypatch.setenv("TRADER_EXECUTION_STRATEGY", "tp5_sl75_daily_core_skip_v1")
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

    def trade_signal(signal_id: int, features: dict[str, object]) -> TradeSignal:
        return TradeSignal(
            id=signal_id,
            symbol=f"S{signal_id}_USDT",
            signaled_at=datetime.now(UTC),
            episode_id=signal_id,
            entry_hint=1.0,
            risk_tier="STANDARD",
            features=features,
        )

    await trader._handle_signal(trade_signal(1, _features()))
    assert decisions[-1] == (1, "ignored_daily_core_filter")
    assert opened == []

    missing = _features()
    missing.pop("daily_ema20_slope")
    await trader._handle_signal(trade_signal(2, missing))
    assert decisions[-1] == (2, "ignored_missing_daily_core_data")
    assert opened == []

    await trader._handle_signal(trade_signal(3, _features(core=False)))
    assert opened == [(3, pytest.approx(100.0))]


@pytest.mark.asyncio
async def test_worker_builds_completed_day1_metadata_on_demand(monkeypatch):
    import sys, types
    sys.modules.setdefault("asyncpg", types.SimpleNamespace())
    from app.config import Settings
    from app.worker import ScannerWorker

    monkeypatch.setenv("DATABASE_URL", "postgresql://example")
    settings = Settings.from_env()
    worker = ScannerWorker(settings)
    confirmed = datetime(2026, 9, 2, 6, 0, tzinfo=UTC)
    candles = []
    price = 1.0
    for i in range(30):
        open_time = confirmed - timedelta(days=31 - i)
        price *= 1.01
        candles.append(Candle("X_USDT", "Day1", open_time, price * 0.99, price * 1.01, price * 0.98, price, 1.0, 1.0))

    async def no_sync(*args, **kwargs):
        return None

    class DB:
        async def fetch_candles(self, symbol, interval, limit):
            assert interval == "Day1"
            return candles[-limit:]

    worker.db = DB()
    monkeypatch.setattr(worker, "_sync_interval", no_sync)
    metadata = await worker._daily_regime_for_confirmed_signal(
        symbol="X_USDT", confirmed_at=confirmed, entry_price=price
    )
    assert metadata["daily_regime_v1_computable"] is True
    assert metadata["daily_regime_v1_bullish"] is True
