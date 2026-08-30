from __future__ import annotations

from datetime import UTC, datetime, timedelta

from app.research_analytics import build_research_analytics, research_strategy_validation_csv
from tests.test_research_analytics_v128 import _base_row


def _standard_cluster() -> list[dict]:
    start = datetime(2026, 8, 2, tzinfo=UTC)
    rows: list[dict] = []
    for idx in range(11):
        row = _base_row(100 + idx, "standard")
        row["confirmed_at"] = start + timedelta(minutes=idx)
        row["target_5_at"] = start + timedelta(hours=24, minutes=idx)
        row["path_latest_return"] = 0.05
        row["adverse_75_at"] = None
        rows.append(row)
    return rows


def test_standard_10_slot_scaling_keeps_admission_fixed_and_scales_exposure():
    rows = _standard_cluster()
    report = build_research_analytics(rows, generated_at=datetime(2026, 8, 5, tzinfo=UTC))

    p5 = report.portfolio_standard_tp5_10
    p75 = report.portfolio_standard_tp5_10x75
    p10 = report.portfolio_standard_tp5_10x10

    assert (p5.entered, p75.entered, p10.entered) == (10, 10, 10)
    assert (p5.missed_capacity, p75.missed_capacity, p10.missed_capacity) == (1, 1, 1)
    assert abs(p5.max_observed_exposure_pct - 0.50) < 1e-12
    assert abs(p75.max_observed_exposure_pct - 0.75) < 1e-12
    assert abs(p10.max_observed_exposure_pct - 1.00) < 1e-12
    assert p5.marked_return < p75.marked_return < p10.marked_return


def test_standard_10pct_sl75_safety_twin_is_identical_without_tail_breach():
    rows = _standard_cluster()
    report = build_research_analytics(rows, generated_at=datetime(2026, 8, 5, tzinfo=UTC))

    assert report.portfolio_standard_tp5_10x10.marked_return == report.portfolio_standard_tp5_sl75_10x10.marked_return
    assert report.portfolio_standard_tp5_10x10.entered == report.portfolio_standard_tp5_sl75_10x10.entered


def test_strategy_validation_csv_exports_standard_scaling_curve():
    report = build_research_analytics(_standard_cluster(), generated_at=datetime(2026, 8, 5, tzinfo=UTC))
    csv_text = research_strategy_validation_csv(report).decode("utf-8")

    assert "standard_tp5_10x5" in csv_text
    assert "standard_tp5_10x7_5" in csv_text
    assert "standard_tp5_10x10" in csv_text
    assert "standard_tp5_sl75_10x10" in csv_text
    assert "10×7.5% / 75% cap" in csv_text
    assert "10×10% / 100% cap" in csv_text
