from __future__ import annotations

from datetime import UTC, datetime, timedelta

from app.performance import build_performance_summary
from tests.test_performance import _row


def _signal(*, episode_id: int, symbol: str, confirmed_at: datetime, pcr: bool, stop: bool):
    row = _row(risk_tier="high_risk", ret24=0.0)
    row.update({
        "episode_id": episode_id,
        "symbol": symbol,
        "confirmed_at": confirmed_at,
        "matured_at": None,
        "current_return_pct": 0.0,
        "target_5_at": None if stop else confirmed_at + timedelta(hours=1),
        "adverse_50_at": confirmed_at + timedelta(minutes=45) if stop else None,
        "adverse_75_at": confirmed_at + timedelta(hours=1) if stop else None,
        "adverse_100_at": None,
        "path_times": [],
        "path_returns": [],
        "feature_snapshot": {
            "return_24h": 0.40 if pcr else 0.20,
            "distance_above_ema20_atr_4h": 3.5 if pcr else 2.0,
        },
    })
    return row


def test_subscriber_pcr_replay_changes_only_position_size_and_reduces_flagged_stop_damage():
    start = datetime(2026, 8, 1, 0, 0, tzinfo=UTC)
    rows = [
        _signal(
            episode_id=1,
            symbol="PCRSTOP_USDT",
            confirmed_at=start,
            pcr=True,
            stop=True,
        ),
        _signal(
            episode_id=2,
            symbol="NORMALWIN_USDT",
            confirmed_at=start + timedelta(days=1),
            pcr=False,
            stop=False,
        ),
    ]

    report = build_performance_summary(
        rows,
        now_utc=start + timedelta(days=2),
        timezone_name="Europe/Zurich",
    )

    fixed = report.tp5_sl75_account_run_rate
    pcr = report.tp5_sl75_pcr_account_run_rate
    assert fixed is not None and pcr is not None

    # Same signal admissions/exits; only the PCR-flagged first trade is half-sized.
    assert pcr.eligible_signals == fixed.eligible_signals == 2
    assert pcr.entered == fixed.entered == 2
    assert pcr.closed == fixed.closed == 2
    assert pcr.missed_capacity == fixed.missed_capacity == 0
    assert pcr.missed_same_symbol == fixed.missed_same_symbol == 0
    assert pcr.observed_account_return > fixed.observed_account_return
    assert pcr.avg_exposure_pct < fixed.avg_exposure_pct
    assert pcr.max_mtm_drawdown < fixed.max_mtm_drawdown
