from datetime import UTC, datetime, timedelta

from app.research_analytics import _calendar_throughput_comparison, _portfolio_replay


def _row(ep: int, at: datetime, t2_h: float, t5_h: float):
    return {
        "episode_id": ep,
        "symbol": f"T{ep}_USDT",
        "risk_tier": "standard",
        "confirmed_at": at,
        "target_2_at": at + timedelta(hours=t2_h),
        "target_5_at": at + timedelta(hours=t5_h),
        "path_latest_return": 0.06,
        "path_mae_before_target_5": -0.03,
        "path_mae_before_target_5_at": at + timedelta(minutes=15),
    }


def test_tp2_replay_uses_same_slots_but_exits_at_two_percent():
    start = datetime(2026, 8, 1, tzinfo=UTC)
    rows = [_row(1, start, 0.5, 4.0)]
    tp2 = _portfolio_replay(rows, strategy="tp2_challenger", generated_at=start + timedelta(days=1))
    tp5 = _portfolio_replay(rows, strategy="tp5_challenger", generated_at=start + timedelta(days=1))
    assert tp2.strategy == "tp2_challenger_6x5pct"
    assert tp2.closed == tp5.closed == 1
    assert tp2.median_holding_hours == 0.5
    assert tp5.median_holding_hours == 4.0
    assert tp2.realized_return < tp5.realized_return


def test_calendar_comparison_contains_tp2_and_faster_recycling_can_reduce_misses():
    start = datetime(2026, 8, 1, tzinfo=UTC)
    rows = [_row(i, start + timedelta(minutes=15 * i), 0.25, 6.0) for i in range(1, 13)]
    report = _calendar_throughput_comparison(
        rows,
        generated_at=start + timedelta(days=2),
        path_rows=(),
    )
    assert report.tp2.strategy == "tp2_challenger_6x5pct"
    assert report.tp2.missed_capacity <= report.tp5.missed_capacity
    assert report.tp2.slot_days < report.tp5.slot_days
