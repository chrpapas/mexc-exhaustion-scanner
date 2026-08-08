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
