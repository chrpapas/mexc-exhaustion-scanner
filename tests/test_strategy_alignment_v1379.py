from datetime import UTC, datetime

from app.config import Settings
from app.performance import current_strategy_signal_is_eligible
from app.strategy_ids import CURRENT_STRATEGY_ID, LEGACY_SUBSCRIBER_V2_ID


def _safe_features():
    return {
        "run_score": 4,
        "distance_above_ema20_atr_4h": 2.0,
        "previous_momentum_1h": -0.01,
        "cross_section_percentile": 0.95,
        "daily_close_above_ema20": False,
        "daily_ema20_slope": -0.01,
        "daily_momentum_3d": -0.02,
        "daily_distance_above_ema20_atr": 1.0,
        "hours_run_to_breakdown": 12.0,
        "lower_high_and_close": True,
        "structural_break_15m": True,
    }


def test_scanner_defaults_to_same_canonical_strategy_as_trader(monkeypatch):
    monkeypatch.setenv("DATABASE_URL", "postgresql://example")
    monkeypatch.delenv("SUBSCRIBER_SIGNAL_STRATEGY", raising=False)
    settings = Settings.from_env()
    assert settings.subscriber_signal_strategy == CURRENT_STRATEGY_ID


def test_scanner_keeps_legacy_v2_alias_for_rollback_compatibility(monkeypatch):
    monkeypatch.setenv("DATABASE_URL", "postgresql://example")
    monkeypatch.setenv("SUBSCRIBER_SIGNAL_STRATEGY", LEGACY_SUBSCRIBER_V2_ID)
    settings = Settings.from_env()
    assert settings.subscriber_signal_strategy == LEGACY_SUBSCRIBER_V2_ID


def test_current_strategy_admission_is_fail_closed_and_shared():
    row = {
        "confirmed_at": datetime(2026, 9, 1, tzinfo=UTC),
        "risk_tier": "standard",
        "feature_snapshot": _safe_features(),
    }
    assert current_strategy_signal_is_eligible(row) is True
    row["feature_snapshot"].pop("daily_momentum_3d")
    assert current_strategy_signal_is_eligible(row) is False
