from datetime import UTC, datetime

from app.performance import build_performance_summary


def test_build_performance_summary_current_day_and_risk_split():
    now = datetime(2026, 8, 10, 10, 0, tzinfo=UTC)
    rows = [
        {
            "episode_id": 1,
            "symbol": "AAA_USDT",
            "confirmed_at": datetime(2026, 8, 10, 7, 0, tzinfo=UTC),
            "entry_price": 10.0,
            "risk_tier": "standard",
            "current_return_pct": 0.05,
            "mfe_pct": 0.10,
            "mae_pct": -0.02,
            "return_1h_pct": 0.02,
            "return_4h_pct": None,
            "return_12h_pct": None,
            "return_24h_pct": None,
            "matured_at": None,
        },
        {
            "episode_id": 2,
            "symbol": "BBB_USDT",
            "confirmed_at": datetime(2026, 8, 8, 7, 0, tzinfo=UTC),
            "entry_price": 20.0,
            "risk_tier": "high_risk",
            "current_return_pct": 0.08,
            "mfe_pct": 0.18,
            "mae_pct": -0.04,
            "return_1h_pct": 0.01,
            "return_4h_pct": 0.04,
            "return_12h_pct": 0.06,
            "return_24h_pct": 0.08,
            "matured_at": datetime(2026, 8, 9, 7, 0, tzinfo=UTC),
        },
    ]

    report = build_performance_summary(
        rows, now_utc=now, timezone_name="Europe/Zurich"
    )
    assert report.report_date.isoformat() == "2026-08-10"
    assert report.confirmed_today == 1
    assert report.open_count == 1
    assert report.open_avg_return == 0.05
    assert report.matured_total == 1
    assert report.high_risk_matured_total == 1
    assert report.win_rate_24h == 1.0
