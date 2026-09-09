from __future__ import annotations

from app.config import Settings
from app.signals import (
    ExhaustionFeatures,
    MarketStateThresholds,
    armed_runner_exhaustion_ready,
    classify_market_state,
    RunFeatures,
)


def _run_features(r24: float, r72: float) -> RunFeatures:
    return RunFeatures(
        return_24h=r24,
        return_72h=r72,
        btc_return_24h=0.0,
        residual_return_24h=r24,
        cross_section_percentile=0.5,
        volume_zscore_15m=0.0,
        distance_above_ema20_atr_4h=0.0,
        amount_24h=5_000_000.0,
        spread_pct=0.1,
        funding_rate=0.0,
        fair_index_premium_pct=0.0,
        hold_vol=1.0,
    )


def _exhaustion(**overrides: object) -> ExhaustionFeatures:
    values: dict[str, object] = {
        "upper_wick_ratio_15m": 0.1,
        "close_location_15m": 0.6,
        "momentum_1h": -0.02,
        "previous_momentum_1h": 0.03,
        "momentum_decelerating": True,
        "below_ema9_15m": True,
        "lower_high_and_close": False,
        "structural_break_15m": False,
        "volume_zscore_15m": 0.0,
    }
    values.update(overrides)
    return ExhaustionFeatures(**values)  # type: ignore[arg-type]


def test_armed_memory_uses_same_active_reversal_structure() -> None:
    thresholds = MarketStateThresholds(active_exhaustion_min_score=2)
    assert armed_runner_exhaustion_ready(_exhaustion(), 2, thresholds)
    assert not armed_runner_exhaustion_ready(
        _exhaustion(momentum_decelerating=False), 2, thresholds
    )
    assert not armed_runner_exhaustion_ready(_exhaustion(), 1, thresholds)


def test_late_prior_runner_classifier_can_admit_below_raw_run_score() -> None:
    thresholds = MarketStateThresholds(min_run_score=3)
    state, _ = classify_market_state(
        _run_features(-0.12, 0.85),
        run_score=2,
        exhaustion_features=_exhaustion(),
        exhaustion_score=2,
        thresholds=thresholds,
    )
    assert state == "exhaustion_watch"


def test_armed_runner_defaults_are_frozen_at_48h(monkeypatch) -> None:
    monkeypatch.setenv("DATABASE_URL", "postgresql://example")
    monkeypatch.delenv("ARMED_RUNNER_MEMORY_HOURS", raising=False)
    monkeypatch.delenv("CONFIRMED_REARM_HOURS", raising=False)
    settings = Settings.from_env()
    assert settings.armed_runner_memory_hours == 48
    assert settings.confirmed_rearm_hours == 48
