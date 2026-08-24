from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from app.performance import build_performance_summary


def _row(*, episode_id: int, symbol: str, risk_tier: str, confirmed: datetime,
         return_7d: float, target_5_hours: float | None) -> dict:
    return {
        "episode_id": episode_id,
        "symbol": symbol,
        "risk_tier": risk_tier,
        "confirmed_at": confirmed,
        "entry_price": 1.0,
        "current_return_pct": return_7d,
        "return_168h_pct": return_7d,
        "matured_168h_at": confirmed + timedelta(days=7),
        "target_5_at": (
            confirmed + timedelta(hours=target_5_hours)
            if target_5_hours is not None else None
        ),
        "path_mae_before_target_5": -0.05 if target_5_hours is not None else None,
    }


def test_public_strategy_metrics_include_wins_losses_and_sums():
    confirmed = datetime(2026, 8, 1, tzinfo=UTC)
    rows = [
        _row(episode_id=1, symbol="STD_WIN", risk_tier="standard", confirmed=confirmed,
             return_7d=0.40, target_5_hours=1.0),
        _row(episode_id=2, symbol="STD_LOSS", risk_tier="standard", confirmed=confirmed + timedelta(hours=1),
             return_7d=-0.10, target_5_hours=None),
        _row(episode_id=3, symbol="HIGH_WIN", risk_tier="high_risk", confirmed=confirmed + timedelta(hours=2),
             return_7d=0.20, target_5_hours=3.0),
    ]

    report = build_performance_summary(
        rows,
        now_utc=confirmed + timedelta(days=10),
        timezone_name="Europe/Zurich",
    )

    tp5 = report.tp5_public
    assert tp5.matured_7d == 3
    assert tp5.hits_7d == 2
    assert tp5.no_tp5_by_7d == 1
    assert tp5.hit_rate_7d == pytest.approx(2 / 3)
    assert tp5.avg_gross_captured_return == pytest.approx(0.10 / 3)
    assert tp5.sum_gross_captured_return == pytest.approx(0.10)

    swing = report.standard_7d_public
    assert swing.matured_7d == 2
    assert swing.wins_7d == 1
    assert swing.losses_7d == 1
    assert swing.positive_rate == pytest.approx(0.5)
    assert swing.avg_return == pytest.approx(0.15)
    assert swing.median_return == pytest.approx(0.15)
    assert swing.sum_return == pytest.approx(0.30)
    assert swing.best_return == pytest.approx(0.40)
    assert swing.worst_return == pytest.approx(-0.10)
