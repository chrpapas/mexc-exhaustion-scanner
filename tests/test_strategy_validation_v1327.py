from __future__ import annotations

from datetime import UTC, datetime, timedelta

from app.performance import build_performance_summary
from app.research_analytics import (
    build_research_analytics,
    research_signal_dataset_csv,
    research_strategy_validation_csv,
)
from tests.test_performance import _row
from tests.test_research_analytics_v128 import _base_row


def test_three_strategy_semantics_keep_indefinite_open_and_cut_7d_at_168h():
    start = datetime(2026, 8, 1, tzinfo=UTC)
    row = _base_row(900, "standard")
    row["confirmed_at"] = start
    row["target_5_at"] = start + timedelta(hours=240)  # day 10
    row["adverse_75_at"] = None
    row["return_168h_pct"] = -0.25
    row["path_return_168h"] = -0.25

    report = build_research_analytics([row], generated_at=start + timedelta(days=12))
    summaries = {item.strategy: item for item in report.strategy_validations}

    assert summaries["tp5_challenger"].target_exits == 1
    assert summaries["tp5_challenger"].timeout_exits == 0
    assert summaries["tp5_sl75_challenger"].target_exits == 1
    assert summaries["hold_7d"].target_exits == 0
    assert summaries["hold_7d"].timeout_exits == 1
    assert summaries["hold_7d"].avg_exit_return == -0.25

    assert report.portfolio_tp5.realized_return > 0
    assert report.portfolio_tp5_sl75.realized_return > 0
    assert report.portfolio_hold_7d.realized_return < 0


def test_strategy_validation_csv_and_signal_dataset_are_llm_ready():
    start = datetime(2026, 8, 1, tzinfo=UTC)
    row = _base_row(901, "high_risk")
    row["confirmed_at"] = start
    row["target_5_at"] = start + timedelta(hours=200)
    row["adverse_75_at"] = start + timedelta(hours=24)
    row["adverse_50_at"] = start + timedelta(hours=12)
    row["adverse_100_at"] = None
    row["return_168h_pct"] = -0.30
    row["path_return_168h"] = -0.30

    generated_at = start + timedelta(days=10)
    report = build_research_analytics([row], generated_at=generated_at)
    summary_text = research_strategy_validation_csv(report).decode("utf-8")
    dataset_text = research_signal_dataset_csv([row], generated_at=generated_at).decode("utf-8")

    assert "tp5_challenger" in summary_text
    assert "tp5_sl75_challenger" in summary_text
    assert "hold_7d" in summary_text
    assert "breach75_later_tp5" in summary_text
    assert "sum_marked_return_pct" in summary_text
    assert "thirty_day_equivalent_return_pct" in summary_text
    assert "capture_rate_pct" in summary_text

    assert "tp5_indefinite_status" in dataset_text
    assert "tp5_sl75_status" in dataset_text
    assert "hold_7d_status" in dataset_text
    assert ",target," in dataset_text
    assert ",stop," in dataset_text
    assert ",timeout," in dataset_text


def test_public_performance_uses_same_entry_universe_for_all_three_exit_policies():
    start = datetime(2026, 8, 1, tzinfo=UTC)
    row = _row(risk_tier="high_risk", ret24=-0.10)
    row["confirmed_at"] = start
    row["target_5_at"] = start + timedelta(hours=200)
    row["adverse_50_at"] = start + timedelta(hours=10)
    row["adverse_75_at"] = start + timedelta(hours=20)
    row["adverse_100_at"] = None
    row["return_168h_pct"] = -0.30
    row["path_return_168h"] = -0.30
    row["current_return_pct"] = 0.05
    row["path_times"] = []
    row["path_returns"] = []

    report = build_performance_summary(
        [row], now_utc=start + timedelta(days=10), timezone_name="Europe/Zurich"
    )

    assert report.trader_strategy_tp5.sample == 1
    assert report.trader_strategy_tp5.target_exits == 1
    assert report.trader_strategy_tp5_sl75.stop_exits == 1
    assert report.trader_strategy_hold_7d.timeout_exits == 1
    assert report.tp5_account_run_rate.eligible_signals == 1
    assert report.tp5_sl75_account_run_rate.eligible_signals == 1
    assert report.hold_7d_account_run_rate.eligible_signals == 1


def test_pure_7d_hold_ignores_early_tp5_and_closes_at_168h_return():
    start = datetime(2026, 8, 1, tzinfo=UTC)
    row = _base_row(902, "standard")
    row["confirmed_at"] = start
    row["target_5_at"] = start + timedelta(hours=2)  # irrelevant to pure 7D hold
    row["return_168h_pct"] = -0.25
    row["path_return_168h"] = -0.25
    row["path_latest_return"] = -0.25

    report = build_research_analytics([row], generated_at=start + timedelta(days=8))
    summaries = {item.strategy: item for item in report.strategy_validations}

    assert summaries["tp5_challenger"].target_exits == 1
    assert summaries["hold_7d"].target_exits == 0
    assert summaries["hold_7d"].timeout_exits == 1
    assert summaries["hold_7d"].avg_exit_return == -0.25
    assert report.portfolio_hold_7d.realized_return < 0
