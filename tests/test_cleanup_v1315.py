from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from app.performance import build_performance_summary


def _paired_high_row(
    episode_id: int,
    confirmed: datetime,
    *,
    timeout_return: float,
    target_hours: float | None,
) -> dict:
    row = {
        "episode_id": episode_id,
        "symbol": f"HIGH_{episode_id}_USDT",
        "risk_tier": "high_risk",
        "confirmed_at": confirmed,
        "entry_price": 1.0,
        "current_return_pct": timeout_return,
        "path_last_at": confirmed + timedelta(hours=240),
        "path_rows_10d": 960,
        "path_return_24h": timeout_return,
        "path_return_48h": timeout_return,
        "path_return_72h": timeout_return,
        "path_return_96h": timeout_return,
        "path_return_120h": timeout_return,
        "path_return_168h": timeout_return,
        "path_return_240h": timeout_return,
        "target_20_path_at": None,
        "target_20_at": None,
    }
    if target_hours is not None:
        row["target_20_path_at"] = confirmed + timedelta(hours=target_hours)
    return row


def test_public_tp20_or_4d_uses_realized_wins_and_paired_10d_cohort():
    confirmed = datetime(2026, 8, 1, tzinfo=UTC)
    rows = [
        _paired_high_row(1, confirmed, timeout_return=-0.30, target_hours=48.0),
        _paired_high_row(2, confirmed + timedelta(hours=1), timeout_return=0.10, target_hours=None),
        _paired_high_row(3, confirmed + timedelta(hours=2), timeout_return=-0.05, target_hours=None),
        # Complete 4d data but not the strict paired 10d cohort: must not leak in.
        {
            **_paired_high_row(4, confirmed + timedelta(hours=3), timeout_return=0.50, target_hours=None),
            "path_last_at": confirmed + timedelta(hours=100),
            "path_rows_10d": 400,
        },
    ]

    report = build_performance_summary(
        rows,
        now_utc=confirmed + timedelta(days=20),
        timezone_name="Europe/Zurich",
    )
    high = report.high_risk_tp20_public

    assert high.sample == 3
    assert high.target_hits == 1
    assert high.target_hit_rate == pytest.approx(1 / 3)
    assert high.wins == 2
    assert high.losses == 1
    assert high.positive_rate == pytest.approx(2 / 3)
    assert high.avg_return == pytest.approx((0.20 + 0.10 - 0.05) / 3)
    assert high.median_return == pytest.approx(0.10)
    assert high.sum_return == pytest.approx(0.25)
    assert high.best_return == pytest.approx(0.20)
    assert high.worst_return == pytest.approx(-0.05)
    assert high.avg_holding_hours == pytest.approx((48 + 96 + 96) / 3)
