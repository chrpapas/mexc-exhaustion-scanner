from datetime import UTC, datetime

import pytest

from app.performance import build_performance_summary


def _row(
    episode_id: int,
    symbol: str,
    risk_tier: str,
    confirmed_at: datetime,
    *,
    r24: float,
    r48: float,
    r72: float,
    r168: float,
    first_profit_at=None,
    target_20_at=None,
    iso=None,
    cross=None,
):
    return {
        "episode_id": episode_id,
        "symbol": symbol,
        "confirmed_at": confirmed_at,
        "entry_price": 1.0,
        "risk_tier": risk_tier,
        "current_return_pct": r168,
        "mfe_pct": max(0.0, r168),
        "mae_pct": -0.30,
        "return_1h_pct": -0.01,
        "return_4h_pct": 0.0,
        "return_12h_pct": 0.02,
        "return_24h_pct": r24,
        "return_48h_pct": r48,
        "return_72h_pct": r72,
        "return_168h_pct": r168,
        "matured_at": confirmed_at.replace(day=confirmed_at.day + 1),
        "matured_48h_at": confirmed_at.replace(day=confirmed_at.day + 2),
        "matured_72h_at": confirmed_at.replace(day=confirmed_at.day + 3),
        "matured_168h_at": confirmed_at.replace(day=confirmed_at.day + 7),
        "first_profit_at": first_profit_at,
        "target_20_at": target_20_at,
        "isolated_100_breach_at": iso,
        "cross_400_breach_at": cross,
    }


def test_weekly_buffer_metrics_separate_standard_and_risky():
    now = datetime(2026, 8, 12, 12, 0, tzinfo=UTC)
    confirmed = datetime(2026, 8, 5, 10, 0, tzinfo=UTC)
    rows = [
        _row(
            1, "STD_USDT", "standard", confirmed,
            r24=0.10, r48=0.20, r72=0.30, r168=0.40,
            first_profit_at=datetime(2026, 8, 5, 12, 0, tzinfo=UTC),
            target_20_at=datetime(2026, 8, 7, 10, 0, tzinfo=UTC),
        ),
        _row(
            2, "RISK_USDT", "high_risk", confirmed,
            r24=-0.20, r48=-0.10, r72=0.10, r168=0.30,
            first_profit_at=datetime(2026, 8, 8, 10, 0, tzinfo=UTC),
            target_20_at=datetime(2026, 8, 10, 10, 0, tzinfo=UTC),
            iso=datetime(2026, 8, 6, 10, 0, tzinfo=UTC),
        ),
    ]
    report = build_performance_summary(rows, now_utc=now, timezone_name="Europe/Zurich")
    assert report.horizon_168h.matured_total == 2
    assert report.standard_weekly.ever_profitable_rate == 1.0
    assert report.standard_weekly.isolated_100_breach_rate == 0.0
    assert report.risky_weekly.isolated_100_breach_rate == 1.0
    assert report.risky_weekly.isolated_breach_before_profit_rate == 1.0
    assert report.risky_weekly.cross_400_breach_rate == 0.0


def test_cross_breach_filters_survivor_returns_by_horizon():
    now = datetime(2026, 8, 12, 12, 0, tzinfo=UTC)
    confirmed = datetime(2026, 8, 5, 10, 0, tzinfo=UTC)
    row = _row(
        3, "X_USDT", "standard", confirmed,
        r24=-0.10, r48=0.05, r72=0.15, r168=0.25,
        first_profit_at=datetime(2026, 8, 7, 10, 0, tzinfo=UTC),
        cross=datetime(2026, 8, 7, 22, 0, tzinfo=UTC),  # 60h after confirmation
    )
    report = build_performance_summary([row], now_utc=now, timezone_name="Europe/Zurich")
    h24, h48, h72, h168 = report.standard_survival
    assert h24.cross_buffer.survived == 1
    assert h48.cross_buffer.survived == 1
    assert h72.cross_buffer.survived == 0
    assert h168.cross_buffer.survived == 0
    assert h48.cross_buffer.avg_return == pytest.approx(0.05)
