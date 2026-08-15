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
        "adverse_200_breach_at": None,
        "adverse_300_breach_at": None,
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

def test_profit_target_race_is_independent_of_fixed_horizons_and_excludes_pending():
    confirmed = datetime(2026, 8, 1, 0, 0, tzinfo=UTC)
    rows = [
        {**_row(risk_tier="standard", ret24=0.25),
         "target_20_at": confirmed.replace(hour=6),
         "isolated_100_breach_at": confirmed.replace(hour=12)},
        {**_row(risk_tier="standard", ret24=-0.40),
         "target_20_at": confirmed.replace(day=3, hour=18),
         "isolated_100_breach_at": confirmed.replace(hour=10)},
        {**_row(risk_tier="standard", ret24=0.00),
         "return_24h_pct": None,
         "matured_at": None,
         "target_20_at": None,
         "isolated_100_breach_at": None},
    ]
    report = build_performance_summary(
        rows, now_utc=datetime(2026, 8, 12, 12, 0, tzinfo=UTC), timezone_name="Europe/Zurich"
    )
    target = report.standard_profit_target
    assert target.isolated.total_signals == 3
    assert target.isolated.resolved == 2
    assert target.isolated.wins == 1
    assert target.isolated.breaches_before_target == 1
    assert target.isolated.pending == 1
    assert target.isolated.win_rate == pytest.approx(0.5)
    assert target.isolated.avg_time_to_target_hours == pytest.approx(6.0)

    # No +400% breach occurred, so both observed +20% targets win the cross race
    # even though the second target arrived well after the 1-day horizon.
    assert target.cross_buffer.resolved == 2
    assert target.cross_buffer.wins == 2
    assert target.cross_buffer.pending == 1
    assert target.cross_buffer.win_rate == pytest.approx(1.0)
    assert target.cross_buffer.avg_time_to_target_hours == pytest.approx((6 + 66) / 2)


def test_same_candle_target_and_breach_is_conservatively_breach_first():
    confirmed = datetime(2026, 8, 1, 0, 0, tzinfo=UTC)
    same = confirmed.replace(hour=8)
    row = {**_row(risk_tier="standard", ret24=0.20),
           "target_20_at": same,
           "isolated_100_breach_at": same}
    report = build_performance_summary(
        [row], now_utc=datetime(2026, 8, 12, 12, 0, tzinfo=UTC), timezone_name="Europe/Zurich"
    )
    assert report.standard_profit_target.isolated.win_rate == 0.0
    assert report.standard_profit_target.isolated.breaches_before_target == 1
    # Cross has no +400% breach, so its target race is still a win.
    assert report.standard_profit_target.cross_buffer.win_rate == 1.0



def test_strategy_matrix_compares_all_thresholds_and_only_exposes_profit_on_perfect_cells():
    confirmed = datetime(2026, 8, 1, 0, 0, tzinfo=UTC)
    winner = {**_row(risk_tier="standard", ret24=0.10),
              "target_20_at": confirmed.replace(hour=8),
              "return_48h_pct": 0.25,
              "matured_48h_at": confirmed.replace(day=3)}
    breached_100 = {**_row(risk_tier="standard", ret24=0.15),
                    "target_20_at": confirmed.replace(day=2, hour=12),
                    "isolated_100_breach_at": confirmed.replace(hour=10),
                    "return_48h_pct": 0.30,
                    "matured_48h_at": confirmed.replace(day=3)}
    report = build_performance_summary(
        [winner, breached_100],
        now_utc=datetime(2026, 8, 12, 12, 0, tzinfo=UTC),
        timezone_name="Europe/Zurich",
    )
    matrix = report.standard_strategy_matrix
    target = matrix.rows[0]
    assert [x.adverse_limit_pct for x in target.thresholds] == [100, 200, 300, 400]
    assert target.thresholds[0].win_rate == pytest.approx(0.5)
    assert target.thresholds[0].avg_profit is None
    assert target.thresholds[1].win_rate == pytest.approx(1.0)
    assert target.thresholds[1].avg_profit == pytest.approx(0.20)
    assert target.thresholds[1].sum_profit == pytest.approx(0.40)

    one_day = matrix.rows[1]
    assert one_day.thresholds[0].win_rate == pytest.approx(0.5)
    assert one_day.thresholds[1].win_rate == pytest.approx(1.0)
    assert one_day.thresholds[0].breach_failures == 1
    assert one_day.thresholds[0].maturity_failures == 0
    assert one_day.thresholds[1].breach_failures == 0
    assert one_day.thresholds[1].maturity_failures == 0
    assert one_day.thresholds[1].avg_profit == pytest.approx(0.125)
    assert one_day.thresholds[1].sum_profit == pytest.approx(0.25)


def test_fixed_horizon_matrix_splits_breach_and_maturity_losses_without_double_counting():
    confirmed = datetime(2026, 8, 1, 0, 0, tzinfo=UTC)
    winner = {**_row(risk_tier="standard", ret24=0.10)}
    negative = {**_row(risk_tier="standard", ret24=-0.15)}
    breached_and_negative = {**_row(risk_tier="standard", ret24=-0.40),
                             "isolated_100_breach_at": confirmed.replace(hour=8)}
    report = build_performance_summary(
        [winner, negative, breached_and_negative],
        now_utc=datetime(2026, 8, 12, 12, 0, tzinfo=UTC),
        timezone_name="Europe/Zurich",
    )
    one_day = report.standard_strategy_matrix.rows[1]
    iso = one_day.thresholds[0]
    buffered = one_day.thresholds[1]
    assert (iso.wins, iso.maturity_failures, iso.breach_failures) == (1, 1, 1)
    assert iso.wins + iso.maturity_failures + iso.breach_failures == iso.total == 3
    assert (buffered.wins, buffered.maturity_failures, buffered.breach_failures) == (1, 2, 0)
    assert buffered.wins + buffered.maturity_failures + buffered.breach_failures == buffered.total == 3
