from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from app.performance import SHADOW_FEE_PER_FILL, build_performance_summary


def _row(
    episode_id: int,
    confirmed: datetime,
    *,
    symbol: str,
    tier: str,
    current: float,
    return_7d: float | None = None,
    target5_h: float | None = None,
    target20_h: float | None = None,
) -> dict:
    def at(hours: float | None):
        return confirmed + timedelta(hours=hours) if hours is not None else None

    return {
        "episode_id": episode_id,
        "symbol": symbol,
        "risk_tier": tier,
        "confirmed_at": confirmed,
        "entry_price": 1.0,
        "current_return_pct": current,
        "return_168h_pct": return_7d,
        "matured_168h_at": confirmed + timedelta(hours=168) if return_7d is not None else None,
        "target_5_at": at(target5_h),
        "target_20_path_at": at(target20_h),
        "target_20_at": at(target20_h),
    }


def test_account_run_rate_replays_actual_rules_with_recommended_sizing_and_linear_30d_equivalent():
    base = datetime(2026, 8, 1, tzinfo=UTC)
    now = base + timedelta(days=10)
    rows = [
        # STANDARD: TP5 closes quickly; 7D closes at the raw +30% mark.
        _row(1, base, symbol="STD1_USDT", tier="standard", current=0.35, return_7d=0.30, target5_h=2),
        # HIGH: TP5 closes +5%; TP20 closes +20% two days later.
        _row(2, base + timedelta(days=1), symbol="HIGH1_USDT", tier="high_risk", current=0.25, return_7d=0.15, target5_h=1, target20_h=48),
        # Recent STANDARD stays open in both TP5 and 7D and is MTM at report time.
        _row(3, base + timedelta(days=9), symbol="STD2_USDT", tier="standard", current=0.04),
        # Recent unresolved HIGH remains open only for TP5/TP20 and is currently underwater.
        _row(4, base + timedelta(days=9, hours=1), symbol="HIGH2_USDT", tier="high_risk", current=-0.10),
    ]

    report = build_performance_summary(rows, now_utc=now, timezone_name="Europe/Zurich")

    tp5 = report.tp5_account_run_rate
    tp20 = report.tp20_account_run_rate
    swing = report.standard_7d_account_run_rate
    assert tp5 is not None and tp20 is not None and swing is not None
    assert tp5.span_days == pytest.approx(10.0)
    assert tp20.span_days == pytest.approx(10.0)
    assert swing.span_days == pytest.approx(10.0)

    assert tp5.entered == 4
    assert tp5.closed == 2
    assert tp5.open_positions == 2
    assert tp20.eligible_signals == 2
    assert tp20.entered == 2
    assert tp20.closed == 1
    assert tp20.open_positions == 1
    assert swing.eligible_signals == 2
    assert swing.entered == 2
    assert swing.closed == 1
    assert swing.open_positions == 1

    # 30D equivalent is deliberately linear from the common 10-day observed span.
    assert tp5.thirty_day_equivalent_return == pytest.approx(tp5.observed_account_return * 3.0)
    assert tp20.thirty_day_equivalent_return == pytest.approx(tp20.observed_account_return * 3.0)
    assert swing.thirty_day_equivalent_return == pytest.approx(swing.observed_account_return * 3.0)
    assert tp5.thirty_day_pnl_per_10k == pytest.approx(tp5.thirty_day_equivalent_return * 10000.0)

    assert tp5.fee_per_fill == pytest.approx(SHADOW_FEE_PER_FILL)
    assert tp5.peak_exposure_pct <= report.tp5_exposure.max_account_exposure_pct
    assert tp20.peak_exposure_pct <= report.tp20_exposure.max_account_exposure_pct
    assert swing.peak_exposure_pct <= report.standard_7d_exposure.max_account_exposure_pct
    assert tp5.avg_exposure_pct is not None
    assert tp20.avg_exposure_pct is not None
    assert swing.avg_exposure_pct is not None


def test_tp20_account_replay_respects_five_slot_cap_and_keeps_unresolved_positions_open():
    base = datetime(2026, 8, 1, tzinfo=UTC)
    now = base + timedelta(days=8)
    rows = [
        _row(
            i,
            base + timedelta(minutes=i),
            symbol=f"H{i}_USDT",
            tier="high_risk",
            current=-0.05 * i,
            return_7d=-0.05 * i,
            target5_h=1,
            target20_h=None,
        )
        for i in range(1, 7)
    ]

    report = build_performance_summary(rows, now_utc=now, timezone_name="Europe/Zurich")
    tp20 = report.tp20_account_run_rate
    assert tp20 is not None
    assert tp20.eligible_signals == 6
    assert tp20.entered == 5
    assert tp20.open_positions == 5
    assert tp20.missed_capacity == 1
    assert tp20.peak_exposure_pct == pytest.approx(0.10)


def test_account_return_is_withheld_if_an_open_position_cannot_be_marked():
    base = datetime(2026, 8, 1, tzinfo=UTC)
    row = _row(1, base, symbol="H_USDT", tier="high_risk", current=0.0)
    row["current_return_pct"] = None
    report = build_performance_summary(
        [row], now_utc=base + timedelta(days=2), timezone_name="Europe/Zurich"
    )
    tp20 = report.tp20_account_run_rate
    assert tp20 is not None
    assert tp20.unmarked_open_positions == 1
    assert tp20.observed_account_return is None
    assert tp20.thirty_day_equivalent_return is None
