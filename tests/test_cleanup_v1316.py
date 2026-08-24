from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from app.performance import build_performance_summary


def _row(
    episode_id: int,
    confirmed: datetime,
    *,
    tier: str,
    return_7d: float,
    target5_h: float | None = None,
    target20_h: float | None = None,
    adverse50_h: float | None = None,
    adverse100_h: float | None = None,
    adverse200_h: float | None = None,
    adverse300_h: float | None = None,
    mae_7d: float = -0.10,
    mae_before_5: float | None = None,
    mae_before_20: float | None = None,
) -> dict:
    def at(hours: float | None):
        return confirmed + timedelta(hours=hours) if hours is not None else None

    return {
        "episode_id": episode_id,
        "symbol": f"S{episode_id}_USDT",
        "risk_tier": tier,
        "confirmed_at": confirmed,
        "entry_price": 1.0,
        "current_return_pct": return_7d,
        "return_168h_pct": return_7d,
        "matured_168h_at": confirmed + timedelta(hours=168),
        "target_5_at": at(target5_h),
        "target_20_path_at": at(target20_h),
        "target_20_at": at(target20_h),
        "adverse_50_at": at(adverse50_h),
        "adverse_100_at": at(adverse100_h),
        "isolated_100_breach_at": at(adverse100_h),
        "adverse_200_breach_at": at(adverse200_h),
        "adverse_300_breach_at": at(adverse300_h),
        "path_mae_7d": mae_7d,
        "path_mae_before_target_5": mae_before_5,
        "path_mae_before_target_20": mae_before_20,
    }


def test_same_168h_mark_compares_tp5_tp20_and_standard_hold_without_forcing_tp20_exit():
    base = datetime(2026, 8, 1, tzinfo=UTC)
    rows = [
        _row(
            1, base, tier="standard", return_7d=0.30,
            target5_h=2, adverse50_h=5, mae_7d=-0.60, mae_before_5=-0.04,
        ),
        _row(
            2, base + timedelta(hours=1), tier="high_risk", return_7d=-0.40,
            target5_h=1, target20_h=200, adverse50_h=50, adverse100_h=100,
            adverse200_h=150, mae_7d=-2.20, mae_before_5=-0.08,
        ),
        _row(
            3, base + timedelta(hours=2), tier="high_risk", return_7d=0.10,
            target5_h=0.5, target20_h=48, adverse50_h=60,
            mae_7d=-0.70, mae_before_5=-0.02, mae_before_20=-0.30,
        ),
        _row(
            4, base + timedelta(hours=3), tier="standard", return_7d=-0.20,
            adverse50_h=80, mae_7d=-0.90,
        ),
    ]

    report = build_performance_summary(
        rows,
        now_utc=base + timedelta(days=20),
        timezone_name="Europe/Zurich",
    )

    tp5 = report.tp5_7d_comparison
    assert tp5 is not None
    assert tp5.sample == 4
    assert tp5.target_hits == 3
    assert tp5.unresolved_at_7d == 1
    assert tp5.wins == 3
    assert tp5.losses == 1
    assert tp5.sum_return == pytest.approx(-0.05)
    # The two later HIGH_RISK breaches happen after TP5 exits and do not count.
    assert tp5.breach_50 == 1
    assert tp5.breach_100 == 0
    assert tp5.breach_200 == 0

    tp20 = report.tp20_7d_comparison
    assert tp20 is not None
    assert tp20.sample == 2
    assert tp20.target_hits == 1
    assert tp20.unresolved_at_7d == 1
    # No-timeout TP20 is only marked at 7d for comparison; the unresolved loss is retained.
    assert tp20.sum_return == pytest.approx(-0.20)
    assert tp20.wins == 1
    assert tp20.losses == 1
    assert tp20.breach_50 == 1
    assert tp20.breach_100 == 1
    assert tp20.breach_200 == 1
    assert tp20.breach_300 == 0
    assert tp20.worst_adverse == pytest.approx(2.20)

    swing = report.standard_7d_comparison
    assert swing is not None
    assert swing.sample == 2
    assert swing.sum_return == pytest.approx(0.10)
    assert swing.wins == 1
    assert swing.losses == 1
    # 7D hold experiences both STANDARD -50% path breaches, including the one after TP5 exit.
    assert swing.breach_50 == 2
    assert swing.avg_effective_holding_hours == pytest.approx(168.0)


def test_exposure_recommendations_are_explicit_and_account_caps_do_not_change_trader_rules():
    base = datetime(2026, 8, 1, tzinfo=UTC)
    report = build_performance_summary(
        [_row(1, base, tier="standard", return_7d=0.10, target5_h=1)],
        now_utc=base + timedelta(days=10),
        timezone_name="Europe/Zurich",
    )

    assert report.tp5_exposure.per_trade_pct == pytest.approx(0.05)
    assert report.tp5_exposure.max_slots == 6
    assert report.tp5_exposure.max_account_exposure_pct == pytest.approx(0.30)
    assert report.tp20_exposure.per_trade_pct == pytest.approx(0.02)
    assert report.tp20_exposure.max_account_exposure_pct == pytest.approx(0.10)
    assert report.standard_7d_exposure.per_trade_pct == pytest.approx(0.03)
    assert report.standard_7d_exposure.max_account_exposure_pct == pytest.approx(0.15)
