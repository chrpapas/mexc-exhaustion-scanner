from datetime import UTC, datetime

from app.performance import build_performance_summary


def test_build_performance_summary_current_day_and_extended_horizons():
    now = datetime(2026, 8, 11, 10, 0, tzinfo=UTC)
    rows = [
        {
            "episode_id": 1,
            "symbol": "AAA_USDT",
            "confirmed_at": datetime(2026, 8, 11, 7, 0, tzinfo=UTC),
            "entry_price": 10.0,
            "risk_tier": "standard",
            "current_return_pct": 0.05,
            "mfe_pct": 0.10,
            "mae_pct": -0.02,
            "return_1h_pct": 0.02,
            "return_4h_pct": None,
            "return_12h_pct": None,
            "return_24h_pct": None,
            "return_48h_pct": None,
            "return_72h_pct": None,
            "matured_at": None,
            "matured_48h_at": None,
            "matured_72h_at": None,
        },
        {
            "episode_id": 2,
            "symbol": "BBB_USDT",
            "confirmed_at": datetime(2026, 8, 8, 7, 0, tzinfo=UTC),
            "entry_price": 20.0,
            "risk_tier": "high_risk",
            "current_return_pct": 0.12,
            "mfe_pct": 0.30,
            "mae_pct": -0.15,
            "return_1h_pct": 0.01,
            "return_4h_pct": -0.04,
            "return_12h_pct": -0.06,
            "return_24h_pct": -0.08,
            "return_48h_pct": 0.03,
            "return_72h_pct": 0.12,
            "matured_at": datetime(2026, 8, 9, 7, 0, tzinfo=UTC),
            "matured_48h_at": datetime(2026, 8, 10, 7, 0, tzinfo=UTC),
            "matured_72h_at": datetime(2026, 8, 11, 7, 0, tzinfo=UTC),
        },
        {
            "episode_id": 3,
            "symbol": "CCC_USDT",
            "confirmed_at": datetime(2026, 8, 9, 8, 0, tzinfo=UTC),
            "entry_price": 5.0,
            "risk_tier": "standard",
            "current_return_pct": 0.09,
            "mfe_pct": 0.18,
            "mae_pct": -0.05,
            "return_1h_pct": -0.02,
            "return_4h_pct": 0.01,
            "return_12h_pct": 0.03,
            "return_24h_pct": 0.04,
            "return_48h_pct": 0.09,
            "return_72h_pct": None,
            "matured_at": datetime(2026, 8, 10, 8, 0, tzinfo=UTC),
            "matured_48h_at": datetime(2026, 8, 11, 8, 0, tzinfo=UTC),
            "matured_72h_at": None,
        },
    ]

    report = build_performance_summary(
        rows, now_utc=now, timezone_name="Europe/Zurich"
    )
    assert report.report_date.isoformat() == "2026-08-11"
    assert report.confirmed_today == 1
    # AAA is new and CCC has not yet reached 72h.
    assert report.open_count == 2
    assert report.horizon_24h.matured_total == 2
    assert report.horizon_48h.matured_total == 2
    assert report.horizon_72h.matured_total == 1
    assert report.horizon_72h.matured_today == 1
    assert report.horizon_72h.win_rate == 1.0
    assert report.horizon_72h.high_risk_total == 1
    assert report.avg_return_72h == 0.12
    assert report.avg_mfe_72h == 0.30
    assert report.avg_mae_72h == -0.15
    assert report.best_symbol_72h == "BBB_USDT"


def test_risky_signal_can_recover_only_after_48h_and_72h():
    now = datetime(2026, 8, 11, 12, 0, tzinfo=UTC)
    rows = [
        {
            "episode_id": 10,
            "symbol": "RISKY_USDT",
            "confirmed_at": datetime(2026, 8, 8, 10, 0, tzinfo=UTC),
            "entry_price": 1.0,
            "risk_tier": "extreme_risk",
            "current_return_pct": 0.40,
            "mfe_pct": 0.55,
            "mae_pct": -0.35,
            "return_1h_pct": -0.10,
            "return_4h_pct": -0.20,
            "return_12h_pct": -0.15,
            "return_24h_pct": -0.08,
            "return_48h_pct": 0.12,
            "return_72h_pct": 0.40,
            "matured_at": datetime(2026, 8, 9, 10, 0, tzinfo=UTC),
            "matured_48h_at": datetime(2026, 8, 10, 10, 0, tzinfo=UTC),
            "matured_72h_at": datetime(2026, 8, 11, 10, 0, tzinfo=UTC),
        }
    ]
    report = build_performance_summary(
        rows, now_utc=now, timezone_name="Europe/Zurich"
    )
    assert report.horizon_24h.win_rate == 0.0
    assert report.horizon_48h.win_rate == 1.0
    assert report.horizon_72h.win_rate == 1.0
    assert report.avg_return_24h == -0.08
    assert report.avg_return_48h == 0.12
    assert report.avg_return_72h == 0.40
