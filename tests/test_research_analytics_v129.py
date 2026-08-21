from __future__ import annotations

from datetime import UTC, datetime, timedelta

from app.research_analytics import build_research_analytics
from tests.test_research_analytics_v128 import _base_row


def test_standard_exit_horizons_use_same_complete_7d_cohort():
    complete = _base_row(1, "standard")
    young = _base_row(2, "standard")
    young["path_last_at"] = young["confirmed_at"] + timedelta(hours=24)
    young["path_rows_7d"] = 96
    young["return_24h_pct"] = 0.90  # must not leak into the paired 1d cohort

    report = build_research_analytics(
        [complete, young], generated_at=datetime(2026, 8, 20, tzinfo=UTC)
    )
    early = [item for item in report.standard_exit_sweep if item.horizon_hours <= 168]
    assert {item.sample for item in early} == {1}
    day1 = next(item for item in early if item.horizon_hours == 24)
    day7 = next(item for item in early if item.horizon_hours == 168)
    assert day1.avg_return == 0.10
    assert day7.avg_return == 0.50
    assert day1.cohort_horizon_hours == 168


def test_high_risk_future_timeout_requires_timeout_maturity_even_after_early_tp():
    now = datetime(2026, 8, 20, 21, 30, tzinfo=UTC)
    young_winner = _base_row(1, "high_risk")
    young_winner["confirmed_at"] = now - timedelta(days=2)
    young_winner["target_20_at"] = young_winner["confirmed_at"] + timedelta(hours=3)
    young_winner["target_20_path_at"] = young_winner["target_20_at"]

    report = build_research_analytics([young_winner], generated_at=now)
    day1 = next(item for item in report.high_risk_timeout_sweep if item.timeout_hours == 24)
    day10 = next(item for item in report.high_risk_timeout_sweep if item.timeout_hours == 240)
    day14 = next(item for item in report.high_risk_timeout_sweep if item.timeout_hours == 336)

    # 1d..10d is now a paired 10d cohort, so a 2-day-old signal is excluded
    # from every comparable timeout rather than entering only the short horizons.
    assert day1.sample == 0
    assert day1.target_hits == 0
    assert day10.sample == 0
    assert day10.target_hits == 0
    assert day14.sample == 0


def test_high_risk_reports_actual_holding_time_and_slot_day_efficiency():
    now = datetime(2026, 8, 20, tzinfo=UTC)
    winner = _base_row(1, "high_risk")
    winner["target_20_at"] = winner["confirmed_at"] + timedelta(hours=10)
    winner["target_20_path_at"] = winner["target_20_at"]
    loser = _base_row(2, "high_risk")
    loser["target_20_at"] = None
    loser["target_20_path_at"] = None
    loser["return_48h_pct"] = -0.10
    loser["path_return_48h"] = -0.10

    report = build_research_analytics([winner, loser], generated_at=now)
    timeout48 = next(item for item in report.high_risk_timeout_sweep if item.timeout_hours == 48)

    assert timeout48.sample == 2
    assert abs(timeout48.avg_holding_hours - 29.0) < 1e-12
    # (+20% - 10%) / ((10h + 48h) / 24h) ~= 4.1379% per occupied slot-day.
    assert abs(timeout48.return_per_slot_day - (0.10 / (58 / 24))) < 1e-12


def test_high_risk_1d_through_10d_use_identical_paired_cohort():
    now = datetime(2026, 8, 20, tzinfo=UTC)
    old_winner = _base_row(1, "high_risk")
    old_winner["confirmed_at"] = now - timedelta(days=12)
    old_winner["target_20_at"] = old_winner["confirmed_at"] + timedelta(hours=30)
    old_winner["target_20_path_at"] = old_winner["target_20_at"]
    # Give the row enough path maturity for all non-TP earlier timeout checks.
    old_winner["path_last_at"] = old_winner["confirmed_at"] + timedelta(days=14)
    old_winner["path_rows_7d"] = 672
    old_winner["path_rows_14d"] = 1344
    for hours in (24, 48, 72, 96, 120, 168, 240, 336):
        old_winner[f"path_rows_{hours}h"] = hours * 4
        old_winner[f"path_return_{hours}h"] = 0.05

    old_loser = _base_row(2, "high_risk")
    old_loser["confirmed_at"] = now - timedelta(days=12)
    old_loser["target_20_at"] = None
    old_loser["target_20_path_at"] = None
    old_loser["path_last_at"] = old_loser["confirmed_at"] + timedelta(days=14)
    old_loser["path_rows_7d"] = 672
    old_loser["path_rows_14d"] = 1344
    for hours in (24, 48, 72, 96, 120, 168, 240, 336):
        old_loser[f"path_rows_{hours}h"] = hours * 4
        old_loser[f"path_return_{hours}h"] = -0.10

    report = build_research_analytics([old_winner, old_loser], generated_at=now)
    paired = [item for item in report.high_risk_timeout_sweep if item.timeout_hours <= 240]
    assert {item.sample for item in paired} == {2}


def test_high_risk_timeout_sweep_strict_pairing_rejects_incomplete_10d_path():
    now = datetime(2026, 8, 20, tzinfo=UTC)
    complete = _base_row(101, "high_risk")
    complete["confirmed_at"] = now - timedelta(days=12)
    complete["target_20_at"] = None
    complete["target_20_path_at"] = None
    complete["path_last_at"] = complete["confirmed_at"] + timedelta(days=10)
    complete["path_rows_7d"] = 672
    complete["path_rows_14d"] = 960
    for hours in (24, 48, 72, 96, 120, 168, 240):
        complete[f"path_return_{hours}h"] = 0.01

    incomplete = _base_row(102, "high_risk")
    incomplete["confirmed_at"] = now - timedelta(days=12)
    incomplete["target_20_at"] = None
    incomplete["target_20_path_at"] = None
    incomplete["path_last_at"] = incomplete["confirmed_at"] + timedelta(days=10)
    incomplete["path_rows_7d"] = 672
    incomplete["path_rows_14d"] = 500  # not enough for a complete 10d 15m path
    for hours in (24, 48, 72, 96, 120, 168, 240):
        incomplete[f"path_return_{hours}h"] = -0.02

    report = build_research_analytics([complete, incomplete], generated_at=now)
    paired = [item for item in report.high_risk_timeout_sweep if item.timeout_hours <= 240]
    assert {item.sample for item in paired} == {1}


def test_high_risk_timeout_sweep_all_1d_to_10d_samples_are_identical():
    now = datetime(2026, 8, 20, tzinfo=UTC)
    rows = []
    for episode_id in range(201, 204):
        row = _base_row(episode_id, "high_risk")
        row["confirmed_at"] = now - timedelta(days=11)
        row["target_20_at"] = None
        row["target_20_path_at"] = None
        row["path_last_at"] = row["confirmed_at"] + timedelta(days=10)
        row["path_rows_7d"] = 672
        row["path_rows_14d"] = 960
        for hours in (24, 48, 72, 96, 120, 168, 240):
            row[f"path_return_{hours}h"] = episode_id / 10000.0
        rows.append(row)

    report = build_research_analytics(rows, generated_at=now)
    paired = [item for item in report.high_risk_timeout_sweep if item.timeout_hours <= 240]
    assert len(paired) == 7
    assert [item.sample for item in paired] == [3] * 7
