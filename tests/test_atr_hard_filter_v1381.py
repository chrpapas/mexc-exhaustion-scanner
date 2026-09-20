from datetime import UTC, datetime

import pytest

from app.strategy_ids import CURRENT_ATR_HARD_MIN_15M_PCT, CURRENT_STRATEGY_ID
from app.trader import _atr_hard_filter_decision
from app.trader_config import TraderSettings
from app.trader_models import TradeSignal


def sig(signal_id: int, *, atr: float | None, entry: float | None) -> TradeSignal:
    features = {"risk_tier": "STANDARD"}
    if atr is not None:
        features["atr_15m"] = atr
    if entry is not None:
        features["retest_close"] = entry
    return TradeSignal(
        id=signal_id,
        symbol=f"S{signal_id}_USDT",
        signaled_at=datetime(2026, 9, 20, 12, 0, tzinfo=UTC),
        episode_id=signal_id,
        entry_hint=entry,
        risk_tier="STANDARD",
        features=features,
    )


def settings(monkeypatch):
    monkeypatch.setenv("DATABASE_URL", "postgresql://example")
    monkeypatch.delenv("TRADER_EXECUTION_STRATEGY", raising=False)
    monkeypatch.delenv("TRADER_PAPER_RUN_ID", raising=False)
    monkeypatch.delenv("ATR_HARD_FILTER_ENABLED", raising=False)
    monkeypatch.delenv("ATR_HARD_FILTER_MIN_ATR_15M_PCT", raising=False)
    return TraderSettings.from_env()


def test_current_strategy_defaults_to_atr_hard_v1(monkeypatch):
    cfg = settings(monkeypatch)
    assert cfg.execution_strategy == CURRENT_STRATEGY_ID
    assert cfg.uses_recovery_runner
    assert cfg.uses_atr_hard_filter
    assert not cfg.uses_atr_capacity_gate
    assert cfg.atr_hard_filter_min_atr_15m_pct == pytest.approx(CURRENT_ATR_HARD_MIN_15M_PCT)
    assert cfg.max_open_positions == 10
    assert cfg.slot_allocation_pct == pytest.approx(10.0)
    assert cfg.max_total_exposure_pct == pytest.approx(100.0)
    assert cfg.paper_run_id == "tp5_adv30_runner50_trail1_atr_hard_v1"


def test_hard_filter_rejects_low_atr_at_zero_occupancy(monkeypatch):
    cfg = settings(monkeypatch)
    ok, reason, value = _atr_hard_filter_decision(sig(1, atr=0.0024, entry=0.1), cfg)
    assert not ok
    assert reason == "below_threshold"
    assert value == pytest.approx(0.024)


def test_hard_filter_accepts_frozen_threshold(monkeypatch):
    cfg = settings(monkeypatch)
    ok, reason, value = _atr_hard_filter_decision(sig(1, atr=0.002461, entry=0.1), cfg)
    assert ok
    assert reason == "passed"
    assert value == pytest.approx(0.02461)


def test_hard_filter_fails_closed_on_missing_atr(monkeypatch):
    cfg = settings(monkeypatch)
    ok, reason, value = _atr_hard_filter_decision(sig(1, atr=None, entry=0.1), cfg)
    assert not ok
    assert reason == "missing_atr_15m_pct"
    assert value is None
