from __future__ import annotations

from datetime import UTC, datetime, timedelta

from app.research_analytics import (
    PARABOLIC_RISK_POSITION_PCT,
    _parabolic_continuation_risk,
    _parabolic_position_fractions,
    build_research_analytics,
    research_volatility_csv,
)
from tests.test_research_analytics_v128 import _base_row


def _row(episode_id: int, at: datetime, r24: float, ema_atr: float, *, stop: bool = False) -> dict:
    row = _base_row(episode_id, "high_risk")
    row["confirmed_at"] = at
    row["entry_price"] = 1.0
    row["feature_snapshot"] = dict(row["feature_snapshot"])
    row["feature_snapshot"]["return_24h"] = r24
    row["feature_snapshot"]["distance_above_ema20_atr_4h"] = ema_atr
    row["path_latest_return"] = -0.80 if stop else 0.05
    row["target_5_at"] = None if stop else at + timedelta(hours=1)
    row["adverse_75_at"] = at + timedelta(hours=2) if stop else None
    row["path_mae_before_target_5"] = -0.80 if stop else -0.10
    return row


def test_parabolic_flag_requires_both_frozen_thresholds():
    at = datetime(2026, 8, 20, tzinfo=UTC)
    assert _parabolic_continuation_risk(_row(1, at, 0.30, 3.0))
    assert not _parabolic_continuation_risk(_row(2, at, 0.2999, 3.0))
    assert not _parabolic_continuation_risk(_row(3, at, 0.30, 2.999))


def test_parabolic_risk_only_downsizes_flagged_signals_and_preserves_cap():
    at = datetime(2026, 8, 20, tzinfo=UTC)
    rows = [
        _row(10, at, 0.35, 3.5, stop=True),
        _row(11, at + timedelta(minutes=1), 0.45, 4.0),
        _row(12, at + timedelta(minutes=2), 0.10, 4.0),
    ]
    fractions = _parabolic_position_fractions(rows)
    assert fractions[10] == PARABOLIC_RISK_POSITION_PCT
    assert fractions[11] == PARABOLIC_RISK_POSITION_PCT
    assert fractions[12] == 0.05

    report = build_research_analytics(rows, generated_at=at + timedelta(days=1))
    v = report.volatility
    assert v.parabolic_flagged_validation.sample == 2
    assert v.parabolic_flagged_validation.stop_exits == 1
    assert v.parabolic_unflagged_validation.sample == 1
    assert v.parabolic_unflagged_validation.stop_exits == 0
    assert v.parabolic_portfolio_de_risked.max_observed_exposure_pct <= 0.30 + 1e-12

    text = research_volatility_csv(report).decode("utf-8")
    assert "parabolic_risk_bucket" in text
    assert "tp5_sl75_parabolic_2_5pct_else_5pct_6slot_30pct" in text
    assert "parabolic_return_24h_threshold_pct" in text
