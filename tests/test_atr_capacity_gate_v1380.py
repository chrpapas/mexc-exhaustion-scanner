from datetime import UTC, datetime

import pytest

from app.strategy_ids import ATR_CAPACITY_GATE_STRATEGY_ID, LEGACY_RECOVERY_RUNNER_ID
from app.trader import _atr_15m_pct, _atr_capacity_gate_decision, _capacity_priority_key
from app.trader_config import TraderSettings
from app.trader_models import TradeSignal


def sig(signal_id: int, *, atr: float | None, entry: float | None, minute: int = 0) -> TradeSignal:
    features = {"risk_tier": "STANDARD"}
    if atr is not None:
        features["atr_15m"] = atr
    if entry is not None:
        features["retest_close"] = entry
    return TradeSignal(
        id=signal_id,
        symbol=f"S{signal_id}_USDT",
        signaled_at=datetime(2026, 9, 20, 12, minute, tzinfo=UTC),
        episode_id=signal_id,
        entry_hint=entry,
        risk_tier="STANDARD",
        features=features,
    )


def settings(monkeypatch, strategy=ATR_CAPACITY_GATE_STRATEGY_ID):
    monkeypatch.setenv("DATABASE_URL", "postgresql://example")
    monkeypatch.setenv("TRADER_EXECUTION_STRATEGY", strategy)
    monkeypatch.setenv("ATR_CAPACITY_GATE_ENABLED", "true")
    return TraderSettings.from_env()


def test_atr_pct_is_frozen_atr_over_signal_entry():
    assert _atr_15m_pct(sig(1, atr=0.002461, entry=0.1)) == pytest.approx(0.02461)


def test_gate_is_inactive_below_seven_slots(monkeypatch):
    cfg = settings(monkeypatch)
    ok, reason, value = _atr_capacity_gate_decision(sig(1, atr=0.001, entry=0.1), cfg, 6)
    assert ok
    assert reason == "not_active"
    assert value == pytest.approx(0.01)


def test_gate_rejects_low_atr_at_seven_slots(monkeypatch):
    cfg = settings(monkeypatch)
    ok, reason, value = _atr_capacity_gate_decision(sig(1, atr=0.0024, entry=0.1), cfg, 7)
    assert not ok
    assert reason == "below_threshold"
    assert value == pytest.approx(0.024)


def test_gate_accepts_threshold_at_seven_slots(monkeypatch):
    cfg = settings(monkeypatch)
    ok, reason, value = _atr_capacity_gate_decision(sig(1, atr=0.002461, entry=0.1), cfg, 7)
    assert ok
    assert reason == "passed"
    assert value == pytest.approx(0.02461)


def test_gate_fails_closed_on_missing_atr_under_scarcity(monkeypatch):
    cfg = settings(monkeypatch)
    ok, reason, value = _atr_capacity_gate_decision(sig(1, atr=None, entry=0.1), cfg, 7)
    assert not ok
    assert reason == "missing_atr_15m_pct"
    assert value is None


def test_same_bucket_prioritizes_higher_atr():
    lo = sig(1, atr=0.002, entry=0.1)
    hi = sig(2, atr=0.004, entry=0.1)
    missing = sig(3, atr=None, entry=0.1)
    ordered = sorted([lo, missing, hi], key=_capacity_priority_key)
    assert [s.id for s in ordered] == [2, 1, 3]


def test_capacity_strategy_remains_available(monkeypatch):
    cfg = settings(monkeypatch)
    assert cfg.uses_recovery_runner
    assert cfg.uses_atr_capacity_gate
    assert not cfg.uses_atr_hard_filter
    assert cfg.paper_run_id == "tp5_adv30_runner50_trail1_atr_gate_v1"


def test_legacy_recovery_runner_remains_available_without_model_filter(monkeypatch):
    cfg = settings(monkeypatch, LEGACY_RECOVERY_RUNNER_ID)
    assert cfg.uses_recovery_runner
    assert not cfg.uses_atr_capacity_gate
    assert not cfg.uses_atr_hard_filter
    assert cfg.paper_run_id == "tp5_adv30_runner50_trail1_candidate_v1"
