from datetime import UTC, datetime, timedelta

import pytest

from app.research_path_aggregation import _aggregate_performance_path_metrics


def test_recovery_runner_path_aggregation_composes_half_tp5_and_half_trail():
    t0 = datetime(2026, 8, 1, tzinfo=UTC)
    rows = [
        {"episode_id": 1, "candle_close_at": t0 + timedelta(minutes=15), "close_return_pct": -0.10, "favorable_return_pct": 0.00, "adverse_return_pct": -0.31},
        {"episode_id": 1, "candle_close_at": t0 + timedelta(minutes=30), "close_return_pct": 0.05, "favorable_return_pct": 0.055, "adverse_return_pct": 0.03},
        {"episode_id": 1, "candle_close_at": t0 + timedelta(minutes=45), "close_return_pct": 0.08, "favorable_return_pct": 0.09, "adverse_return_pct": 0.07},
        {"episode_id": 1, "candle_close_at": t0 + timedelta(minutes=60), "close_return_pct": 0.075, "favorable_return_pct": 0.085, "adverse_return_pct": 0.075},
    ]
    out = _aggregate_performance_path_metrics(t0, rows)
    assert out["recovery_runner_triggered"] is True
    assert out["target_5_at"] == t0 + timedelta(minutes=30)
    assert out["recovery_runner_exit_at"] == t0 + timedelta(minutes=45)
    # High-water 9%, 1pp trail = 8%; composite = half 5% + half 8% = 6.5%.
    assert out["recovery_runner_exit_return"] == pytest.approx(0.065)


def test_recovery_runner_does_not_trigger_without_30pct_pre_tp5_adverse():
    t0 = datetime(2026, 8, 1, tzinfo=UTC)
    rows = [
        {"episode_id": 1, "candle_close_at": t0 + timedelta(minutes=15), "close_return_pct": -0.10, "favorable_return_pct": 0.00, "adverse_return_pct": -0.29},
        {"episode_id": 1, "candle_close_at": t0 + timedelta(minutes=30), "close_return_pct": 0.05, "favorable_return_pct": 0.051, "adverse_return_pct": 0.04},
    ]
    out = _aggregate_performance_path_metrics(t0, rows)
    assert out["recovery_runner_triggered"] is False
    assert out["recovery_runner_exit_at"] is None
