from datetime import UTC, date, datetime

import pytest

from app.performance import short_return, should_send_daily_report


def test_short_return_profit_when_price_falls():
    assert short_return(100.0, 90.0) == pytest.approx(0.10)


def test_short_return_loss_when_price_rises():
    assert short_return(100.0, 110.0) == pytest.approx(-0.10)


def test_daily_report_waits_until_local_hour():
    # 15:00 UTC in August is 17:00 Europe/Zurich.
    now = datetime(2026, 8, 8, 15, 0, tzinfo=UTC)
    assert not should_send_daily_report(
        now,
        timezone_name="Europe/Zurich",
        report_hour=18,
        already_sent_date=None,
    )


def test_daily_report_sends_once_per_local_day():
    # 16:05 UTC in August is 18:05 Europe/Zurich.
    now = datetime(2026, 8, 8, 16, 5, tzinfo=UTC)
    assert should_send_daily_report(
        now,
        timezone_name="Europe/Zurich",
        report_hour=18,
        already_sent_date=None,
    )
    assert not should_send_daily_report(
        now,
        timezone_name="Europe/Zurich",
        report_hour=18,
        already_sent_date=date(2026, 8, 8),
    )

from app.performance import build_performance_summary


def _row(*, risk_tier: str, ret24: float, iso_breach=None, cross_breach=None):
    confirmed = datetime(2026, 8, 1, 0, 0, tzinfo=UTC)
    return {
        "episode_id": 1,
        "symbol": "TEST_USDT",
        "confirmed_at": confirmed,
        "entry_price": 1.0,
        "risk_tier": risk_tier,
        "current_return_pct": ret24,
        "mfe_pct": 0.2,
        "mae_pct": -0.5,
        "return_1h_pct": None,
        "return_4h_pct": None,
        "return_12h_pct": None,
        "return_24h_pct": ret24,
        "return_48h_pct": None,
        "return_72h_pct": None,
        "return_168h_pct": None,
        "matured_at": confirmed.replace(day=2),
        "matured_48h_at": None,
        "matured_72h_at": None,
        "matured_168h_at": None,
        "first_profit_at": None,
        "target_20_at": None,
        "isolated_100_breach_at": iso_breach,
        "cross_400_breach_at": cross_breach,
    }


def test_horizon_survival_separates_isolated_and_cross_thresholds():
    confirmed = datetime(2026, 8, 1, 0, 0, tzinfo=UTC)
    rows = [
        _row(risk_tier="standard", ret24=0.10, iso_breach=confirmed.replace(hour=12), cross_breach=None),
        _row(risk_tier="standard", ret24=0.20, iso_breach=None, cross_breach=None),
    ]
    report = build_performance_summary(
        rows, now_utc=datetime(2026, 8, 12, 12, 0, tzinfo=UTC), timezone_name="Europe/Zurich"
    )
    h = report.standard_survival[0]
    assert h.matured_total == 2
    assert h.isolated.survived == 1
    assert h.isolated.survival_rate == pytest.approx(0.5)
    assert h.isolated.avg_return == pytest.approx(0.20)
    assert h.cross_buffer.survived == 2
    assert h.cross_buffer.survival_rate == pytest.approx(1.0)
    assert h.cross_buffer.avg_return == pytest.approx(0.15)
