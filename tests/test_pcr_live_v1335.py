from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace

import pytest

from app.trader_config import TraderSettings
from app.trader_logic import (
    PCR_BASE_POSITION_FRACTION,
    PCR_EMA_DISTANCE_ATR_THRESHOLD,
    PCR_FLAGGED_POSITION_FRACTION,
    PCR_RETURN_24H_THRESHOLD,
    parabolic_continuation_risk,
    pcr_position_fraction,
)
from app.trader_models import TradeSignal


def _signal(*, signal_id: int, r24: float | None, extension: float | None) -> TradeSignal:
    features = {}
    if r24 is not None:
        features["return_24h"] = r24
    if extension is not None:
        features["distance_above_ema20_atr_4h"] = extension
    return TradeSignal(
        id=signal_id, symbol=f"PCR{signal_id}_USDT", signaled_at=datetime.now(UTC),
        episode_id=signal_id, entry_hint=1.0, risk_tier="HIGH_RISK", features=features,
    )


def test_pcr_frozen_boundaries_and_missing_features():
    assert PCR_RETURN_24H_THRESHOLD == pytest.approx(0.30)
    assert PCR_EMA_DISTANCE_ATR_THRESHOLD == pytest.approx(3.0)
    assert PCR_FLAGGED_POSITION_FRACTION == pytest.approx(0.025)
    assert PCR_BASE_POSITION_FRACTION == pytest.approx(0.05)
    assert parabolic_continuation_risk({"return_24h": 0.30, "distance_above_ema20_atr_4h": 3.0})
    assert not parabolic_continuation_risk({"return_24h": 0.299999, "distance_above_ema20_atr_4h": 3.0})
    assert not parabolic_continuation_risk({"return_24h": 0.30, "distance_above_ema20_atr_4h": 2.999999})
    assert not parabolic_continuation_risk({"return_24h": 0.40})
    assert pcr_position_fraction({"return_24h": 0.30, "distance_above_ema20_atr_4h": 3.0}) == pytest.approx(0.025)
    assert pcr_position_fraction({"return_24h": 0.29, "distance_above_ema20_atr_4h": 4.0}) == pytest.approx(0.05)


def test_daily_core_skip_is_new_default_but_pcr_and_fixed_sl75_remain_supported(monkeypatch):
    monkeypatch.setenv("DATABASE_URL", "postgresql://example")
    monkeypatch.delenv("TRADER_EXECUTION_STRATEGY", raising=False)
    monkeypatch.delenv("TRADER_PAPER_RUN_ID", raising=False)
    settings = TraderSettings.from_env()
    assert settings.execution_strategy == "tp5_sl75_daily_core_skip_v1"
    assert settings.paper_run_id == "tp5_sl75_daily_core_skip_v1"
    assert settings.uses_daily_core_skip
    assert not settings.uses_pcr_derisk
    assert settings.uses_catastrophic_stop
    assert settings.uses_generic_slots
    assert settings.max_open_positions == 6
    assert settings.max_total_exposure_pct == pytest.approx(30.0)

    monkeypatch.setenv("TRADER_EXECUTION_STRATEGY", "tp5_sl75_v1")
    fixed = TraderSettings.from_env()
    assert not fixed.uses_pcr_derisk
    assert fixed.uses_catastrophic_stop


@pytest.mark.asyncio
async def test_live_handler_uses_2_5pct_flagged_and_5pct_unflagged(monkeypatch):
    import sys, types
    sys.modules.setdefault("asyncpg", types.SimpleNamespace())
    from app.trader import PortfolioShortTrader

    monkeypatch.setenv("DATABASE_URL", "postgresql://example")
    monkeypatch.setenv("TRADER_EXECUTION_STRATEGY", "tp5_sl75_pcr_v1")
    settings = TraderSettings.from_env()
    trader = PortfolioShortTrader(settings)
    active = []
    opened: list[tuple[int, float]] = []

    class Repo:
        async def active_positions(self):
            return active
        async def decision(self, signal_id, decision, reason, position_id=None):
            raise AssertionError(f"unexpected decision {decision}: {reason}")

    trader.repo = Repo()
    async def equity(_active=None):
        return 2000.0
    async def open_paper(signal, slot_no, equity_value, notional):
        opened.append((signal.id, notional))
        return SimpleNamespace(
            id=signal.id, symbol=signal.symbol, risk_tier=signal.risk_tier, slot_no=slot_no,
            notional_usdt=notional, mode="paper", entry_price=1.0,
        )
    async def quiet(*args, **kwargs):
        return None
    monkeypatch.setattr(trader, "_account_equity", equity)
    monkeypatch.setattr(trader, "_open_paper", open_paper)
    monkeypatch.setattr(trader, "_notify", quiet)

    await trader._handle_signal(_signal(signal_id=1, r24=0.30, extension=3.0))
    await trader._handle_signal(_signal(signal_id=2, r24=0.29, extension=4.0))
    assert opened[0][1] == pytest.approx(50.0)
    assert opened[1][1] == pytest.approx(100.0)


@pytest.mark.asyncio
async def test_pcr_flagged_can_use_last_2_5pct_capacity_but_normal_5pct_cannot(monkeypatch):
    import sys, types
    sys.modules.setdefault("asyncpg", types.SimpleNamespace())
    from app.trader import PortfolioShortTrader

    monkeypatch.setenv("DATABASE_URL", "postgresql://example")
    monkeypatch.setenv("TRADER_EXECUTION_STRATEGY", "tp5_sl75_pcr_v1")
    settings = TraderSettings.from_env()

    def existing_positions():
        # Five slots using 27.5% of a $2,000 account, leaving exactly 2.5% capacity.
        return [
            SimpleNamespace(
                symbol=f"OLD{i}_USDT", risk_tier="STANDARD", slot_no=i,
                notional_usdt=110.0, mode="paper",
            )
            for i in range(1, 6)
        ]

    async def run_case(signal: TradeSignal):
        trader = PortfolioShortTrader(settings)
        active = existing_positions()
        decisions: list[tuple[str, str]] = []
        opened: list[float] = []

        class Repo:
            async def active_positions(self):
                return active
            async def decision(self, signal_id, decision, reason, position_id=None):
                decisions.append((decision, reason))

        trader.repo = Repo()
        async def equity(_active=None):
            return 2000.0
        async def open_paper(signal, slot_no, equity_value, notional):
            opened.append(notional)
            return SimpleNamespace(
                id=signal.id, symbol=signal.symbol, risk_tier=signal.risk_tier, slot_no=slot_no,
                notional_usdt=notional, mode="paper", entry_price=1.0,
            )
        async def quiet(*args, **kwargs):
            return None
        monkeypatch.setattr(trader, "_account_equity", equity)
        monkeypatch.setattr(trader, "_open_paper", open_paper)
        monkeypatch.setattr(trader, "_notify", quiet)
        await trader._handle_signal(signal)
        return opened, decisions

    flagged_opened, flagged_decisions = await run_case(_signal(signal_id=10, r24=0.30, extension=3.0))
    assert flagged_opened == [pytest.approx(50.0)]
    assert flagged_decisions == []

    normal_opened, normal_decisions = await run_case(_signal(signal_id=11, r24=0.29, extension=4.0))
    assert normal_opened == []
    assert normal_decisions and normal_decisions[0][0] == "ignored_exposure"
