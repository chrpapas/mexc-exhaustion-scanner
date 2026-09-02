from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace

import pytest

from app.trader_config import TraderSettings
from app.trader_logic import (
    HTF_BASE_POSITION_FRACTION,
    HTF_CROSS_SECTION_PERCENTILE_THRESHOLD,
    HTF_EMA_DISTANCE_ATR_THRESHOLD,
    HTF_FLAGGED_POSITION_FRACTION,
    HTF_RETURN_24H_THRESHOLD,
    htf_continuation_risk,
    htf_missing_features,
    htf_position_fraction,
    htf_snapshot_metadata,
)
from app.trader_models import TradeSignal


def _features(**updates):
    values = {
        "return_24h": 0.30,
        "cross_section_percentile": 0.98,
        "distance_above_ema20_atr_4h": 3.0,
        "previous_momentum_1h": 0.01,
    }
    values.update(updates)
    return values


def _signal(signal_id: int, features: dict[str, object]) -> TradeSignal:
    return TradeSignal(
        id=signal_id,
        symbol=f"HTF{signal_id}_USDT",
        signaled_at=datetime.now(UTC),
        episode_id=signal_id,
        entry_hint=1.0,
        risk_tier="HIGH_RISK",
        features=features,
    )


def test_htf_v1_frozen_boundaries_and_missing_are_tristate():
    assert HTF_RETURN_24H_THRESHOLD == pytest.approx(0.30)
    assert HTF_CROSS_SECTION_PERCENTILE_THRESHOLD == pytest.approx(0.98)
    assert HTF_EMA_DISTANCE_ATR_THRESHOLD == pytest.approx(3.0)
    assert HTF_FLAGGED_POSITION_FRACTION == pytest.approx(0.025)
    assert HTF_BASE_POSITION_FRACTION == pytest.approx(0.05)

    assert htf_continuation_risk(_features()) is True
    assert htf_continuation_risk(_features(return_24h=0.2999)) is False
    assert htf_continuation_risk(_features(cross_section_percentile=0.9799)) is False
    assert htf_continuation_risk(_features(distance_above_ema20_atr_4h=2.9999)) is False
    assert htf_continuation_risk(_features(previous_momentum_1h=0.0)) is False

    missing = _features()
    missing.pop("cross_section_percentile")
    assert htf_continuation_risk(missing) is None
    assert htf_position_fraction(missing) is None
    assert htf_missing_features(missing) == ("cross_section_percentile",)
    metadata = htf_snapshot_metadata(missing)
    assert metadata["htf_v1_computable"] is False
    assert metadata["htf_v1_flagged"] is None
    assert metadata["htf_v1_missing_fields"] == ["cross_section_percentile"]


def test_htf_strategy_supported_but_daily_core_skip_remains_default(monkeypatch):
    monkeypatch.setenv("DATABASE_URL", "postgresql://example")
    monkeypatch.delenv("TRADER_EXECUTION_STRATEGY", raising=False)
    default = TraderSettings.from_env()
    assert default.execution_strategy == "tp5_sl75_daily_core_skip_v1"
    assert default.uses_daily_core_skip
    assert not default.uses_pcr_derisk
    assert not default.uses_htf_derisk

    monkeypatch.setenv("TRADER_EXECUTION_STRATEGY", "tp5_sl75_htf_v1")
    htf = TraderSettings.from_env()
    assert htf.uses_htf_derisk
    assert not htf.uses_pcr_derisk
    assert htf.uses_catastrophic_stop
    assert htf.uses_generic_slots
    assert htf.max_open_positions == 6
    assert htf.max_total_exposure_pct == pytest.approx(30.0)


@pytest.mark.asyncio
async def test_htf_live_path_halves_flagged_size_and_fails_closed_on_missing(monkeypatch):
    import sys, types
    sys.modules.setdefault("asyncpg", types.SimpleNamespace())
    from app.trader import PortfolioShortTrader

    monkeypatch.setenv("DATABASE_URL", "postgresql://example")
    monkeypatch.setenv("TRADER_EXECUTION_STRATEGY", "tp5_sl75_htf_v1")
    monkeypatch.setenv("TRADER_PAPER_RUN_ID", "htf-test")
    settings = TraderSettings.from_env()
    trader = PortfolioShortTrader(settings)
    opened: list[float] = []
    decisions: list[tuple[str, str]] = []

    class Repo:
        async def active_positions(self):
            return []
        async def decision(self, signal_id, decision, reason, position_id=None):
            decisions.append((decision, reason))

    trader.repo = Repo()

    async def equity(_active=None):
        return 2000.0

    async def open_paper(signal, slot_no, equity_value, notional):
        opened.append(notional)
        return SimpleNamespace(
            id=signal.id, symbol=signal.symbol, risk_tier=signal.risk_tier,
            slot_no=slot_no, notional_usdt=notional, mode="paper", entry_price=1.0,
        )

    async def quiet(*args, **kwargs):
        return None

    monkeypatch.setattr(trader, "_account_equity", equity)
    monkeypatch.setattr(trader, "_open_paper", open_paper)
    monkeypatch.setattr(trader, "_notify", quiet)

    await trader._handle_signal(_signal(1, _features()))
    assert opened == [pytest.approx(50.0)]

    missing = _features()
    missing.pop("previous_momentum_1h")
    await trader._handle_signal(_signal(2, missing))
    assert len(opened) == 1
    assert decisions[-1][0] == "ignored_missing_htf_data"
    assert "previous_momentum_1h" in decisions[-1][1]


def test_candle_backfill_reconstructs_causal_inputs_without_cross_section():
    from datetime import timedelta
    from app.htf_backfill import reconstruct_candle_htf_features

    confirmed = datetime(2026, 9, 1, 12, 7, tzinfo=UTC)
    # 110 completed 15m candles ending at 12:00 UTC. Prices rise steadily so
    # both the prior-hour momentum and 24h proxy are deterministic and positive.
    min15 = []
    start = confirmed.replace(minute=0, second=0, microsecond=0) - timedelta(minutes=15 * 109)
    for index in range(110):
        open_time = start + timedelta(minutes=15 * index)
        price = 1.0 + 0.001 * index
        min15.append({"open_time": open_time, "close": price, "high": price + 0.01, "low": price - 0.01})

    hour4 = []
    start4 = confirmed.replace(hour=8, minute=0, second=0, microsecond=0) - timedelta(hours=4 * 79)
    for index in range(80):
        open_time = start4 + timedelta(hours=4 * index)
        price = 1.0 + 0.01 * index
        hour4.append({"open_time": open_time, "close": price, "high": price + 0.02, "low": price - 0.02})

    recovered, sources = reconstruct_candle_htf_features(
        confirmed_at=confirmed,
        entry_price=1.20,
        min15_rows=min15,
        hour4_rows=hour4,
    )
    assert recovered["previous_momentum_1h"] > 0
    assert recovered["return_24h"] > 0
    assert "distance_above_ema20_atr_4h" in recovered
    assert sources["previous_momentum_1h"] == "min15_reconstruction"
    assert sources["return_24h"] == "min15_24h_proxy"
    assert sources["distance_above_ema20_atr_4h"] == "hour4_reconstruction"
    assert "cross_section_percentile" not in recovered


def test_pcr_plus_htf_flags_union_only_on_computable_rows():
    from app.research_analytics import _pcr_plus_htf_position_fractions

    rows = [
        {"episode_id": 1, "feature_snapshot": _features()},  # both PCR + HTF
        {"episode_id": 2, "feature_snapshot": _features(previous_momentum_1h=-0.01)},  # PCR only
        {"episode_id": 3, "feature_snapshot": _features(return_24h=0.20)},  # neither
        {"episode_id": 4, "feature_snapshot": {"return_24h": 0.50}},  # HTF non-computable
    ]
    fractions = _pcr_plus_htf_position_fractions(rows)
    assert fractions[1] == pytest.approx(0.025)
    assert fractions[2] == pytest.approx(0.025)
    assert fractions[3] == pytest.approx(0.05)
    assert 4 not in fractions


def test_htf_v1_is_strict_subset_of_pcr_v1_by_construction():
    from app.trader_logic import parabolic_continuation_risk

    # HTF V1 shares PCR's 24h and 4h-extension thresholds and adds two more
    # requirements, so any HTF-flagged signal must already be PCR-flagged.
    candidates = [
        _features(),
        _features(cross_section_percentile=0.99, previous_momentum_1h=0.20),
        _features(return_24h=0.50, distance_above_ema20_atr_4h=8.0),
    ]
    for features in candidates:
        if htf_continuation_risk(features) is True:
            assert parabolic_continuation_risk(features) is True
