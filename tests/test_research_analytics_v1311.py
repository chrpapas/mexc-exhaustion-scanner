from datetime import UTC, datetime, timedelta

from app.research_analytics import _calendar_throughput_comparison, _portfolio_replay


def _row(ep: int, at: datetime, *, t1_h: float = 0.25, t2_h: float = 0.5, t5_h: float = 6.0):
    return {
        "episode_id": ep,
        "symbol": f"V{ep}_USDT",
        "risk_tier": "standard",
        "confirmed_at": at,
        "target_1_at": at + timedelta(hours=t1_h),
        "target_2_at": at + timedelta(hours=t2_h),
        "target_5_at": at + timedelta(hours=t5_h),
        "path_latest_return": 0.06,
        "path_mae_before_target_5": -0.03,
        "path_mae_before_target_5_at": at + timedelta(minutes=15),
    }


def test_tp1_10_uses_one_percent_target_and_ten_five_percent_slots():
    start = datetime(2026, 8, 1, tzinfo=UTC)
    rows = [_row(i, start, t1_h=4.0, t2_h=8.0, t5_h=12.0) for i in range(1, 12)]
    tp1 = _portfolio_replay(
        rows,
        strategy="tp1_10_challenger",
        generated_at=start + timedelta(days=1),
    )
    assert tp1.strategy == "tp1_challenger_10x5pct"
    assert tp1.entered == 10
    assert tp1.missed_capacity == 1
    assert tp1.max_open_positions == 10
    assert tp1.max_observed_exposure_pct == 0.50
    assert tp1.closed == 10
    assert tp1.median_holding_hours == 4.0


def test_tp2_10_has_more_capacity_than_tp2_6_on_same_burst():
    start = datetime(2026, 8, 1, tzinfo=UTC)
    rows = [_row(i, start, t2_h=6.0, t5_h=12.0) for i in range(1, 11)]
    tp2_6 = _portfolio_replay(
        rows,
        strategy="tp2_challenger",
        generated_at=start + timedelta(days=1),
    )
    tp2_10 = _portfolio_replay(
        rows,
        strategy="tp2_10_challenger",
        generated_at=start + timedelta(days=1),
    )
    assert tp2_6.strategy == "tp2_challenger_6x5pct"
    assert tp2_10.strategy == "tp2_challenger_10x5pct"
    assert tp2_6.entered == 6
    assert tp2_6.missed_capacity == 4
    assert tp2_10.entered == 10
    assert tp2_10.missed_capacity == 0
    assert tp2_10.max_observed_exposure_pct == 0.50


def test_calendar_comparison_contains_tp5_6_tp2_10_and_tp1_10():
    start = datetime(2026, 8, 1, tzinfo=UTC)
    rows = [
        _row(i, start + timedelta(minutes=15 * i), t1_h=0.25, t2_h=0.5, t5_h=6.0)
        for i in range(1, 15)
    ]
    report = _calendar_throughput_comparison(
        rows,
        generated_at=start + timedelta(days=2),
        path_rows=(),
    )
    assert report.tp5.strategy == "tp5_challenger_6x5pct"
    assert report.tp2_10.strategy == "tp2_challenger_10x5pct"
    assert report.tp1_10.strategy == "tp1_challenger_10x5pct"
    assert report.tp1_10.missed_capacity <= report.tp5.missed_capacity
    assert report.tp2_10.missed_capacity <= report.tp5.missed_capacity
