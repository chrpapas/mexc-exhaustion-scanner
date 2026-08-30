from __future__ import annotations

from datetime import UTC, datetime, timedelta

from app.research_analytics import (
    RESEARCH_OOS_FREEZE_AT,
    _entry_atr_pct,
    _volatility_position_fractions,
    build_research_analytics,
    research_signal_dataset_csv,
    research_volatility_csv,
)
from tests.test_research_analytics_v128 import _base_row


def _row_with_atr(episode_id: int, at: datetime, atr_pct: float, *, tier: str = "standard") -> dict:
    row = _base_row(episode_id, tier)
    row["confirmed_at"] = at
    row["entry_price"] = 1.0
    row["feature_snapshot"] = dict(row["feature_snapshot"])
    row["feature_snapshot"]["atr_15m"] = atr_pct
    row["target_5_at"] = at + timedelta(hours=2)
    row["path_latest_return"] = 0.05
    row["path_mae_before_target_5"] = -0.10
    return row


def test_atr_pct_is_signal_time_normalized_and_exported():
    row = _row_with_atr(1001, datetime(2026, 8, 10, tzinfo=UTC), 0.025)
    row["entry_price"] = 2.0
    assert abs(_entry_atr_pct(row) - 0.0125) < 1e-12

    text = research_signal_dataset_csv([row], generated_at=datetime(2026, 8, 11, tzinfo=UTC)).decode("utf-8")
    assert "atr_15m" in text.splitlines()[0]
    assert "atr_15m_pct" in text.splitlines()[0]
    assert ",0.025," in text
    assert ",0.0125," in text


def test_volatility_anchor_is_frozen_from_pre_freeze_only():
    discovery = [
        _row_with_atr(1010 + i, datetime(2026, 8, 10, tzinfo=UTC) + timedelta(hours=i), atr)
        for i, atr in enumerate((0.01, 0.02, 0.03, 0.04))
    ]
    post = _row_with_atr(1020, RESEARCH_OOS_FREEZE_AT + timedelta(hours=1), 0.20, tier="high_risk")
    report = build_research_analytics(discovery + [post], generated_at=datetime(2026, 8, 30, tzinfo=UTC))

    assert report.volatility.calibration_sample == 4
    assert abs(report.volatility.calibration_median - 0.025) < 1e-12
    assert report.volatility.observed_sample == 5
    assert report.volatility.missing_atr == 0

    fractions = _volatility_position_fractions([post], calibration_median=report.volatility.calibration_median)
    assert abs(fractions[1020] - 0.025) < 1e-12  # high vol clamps to the 2.5% floor


def test_atr_normalized_sizing_clamps_and_respects_30pct_total_cap():
    start = datetime(2026, 8, 10, tzinfo=UTC)
    rows = [
        _row_with_atr(1030 + i, start + timedelta(minutes=i), atr, tier="high_risk" if i % 2 else "standard")
        for i, atr in enumerate((0.005, 0.01, 0.02, 0.03, 0.05, 0.10, 0.20))
    ]
    report = build_research_analytics(rows, generated_at=datetime(2026, 8, 11, tzinfo=UTC))
    v = report.volatility

    assert v.calibration_median is not None
    fractions = _volatility_position_fractions(rows, calibration_median=v.calibration_median)
    assert min(fractions.values()) >= 0.025
    assert max(fractions.values()) <= 0.075
    assert v.portfolio_normalized.max_observed_exposure_pct <= 0.30 + 1e-12
    assert v.portfolio_normalized.max_open_positions <= 6
    assert v.portfolio_fixed.marked_return == report.portfolio_tp5_sl75.marked_return
    assert v.portfolio_fixed.entered == report.portfolio_tp5_sl75.entered


def test_volatility_csv_contains_quartiles_and_fixed_vs_normalized_portfolios():
    start = datetime(2026, 8, 10, tzinfo=UTC)
    rows = [
        _row_with_atr(1040 + i, start + timedelta(hours=i), atr, tier="standard" if i < 4 else "high_risk")
        for i, atr in enumerate((0.01, 0.015, 0.02, 0.025, 0.03, 0.04, 0.06, 0.10))
    ]
    report = build_research_analytics(rows, generated_at=datetime(2026, 8, 12, tzinfo=UTC))
    text = research_volatility_csv(report).decode("utf-8")

    assert "Q1_low" in text
    assert "Q4_high" in text
    assert "tp5_sl75_fixed_6x5_30pct" in text
    assert "tp5_sl75_atr_normalized_6slot_30pct" in text
    assert "calibration_median_atr_pct" in text
