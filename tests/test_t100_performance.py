from datetime import UTC, datetime, timedelta

import pytest

from app.t100_performance import build_t100_performance_summary


def test_t100_dashboard_uses_net_costs_funding_and_open_mtm():
    now = datetime(2026, 10, 7, 16, 0, tzinfo=UTC)
    runtime = {
        "starting_equity_usdt": 2000.0,
        "realized_equity_usdt": 1990.0,
        "started_at": now - timedelta(days=2),
    }
    positions = [
        {
            "status": "open",
            "slot_no": 1,
            "symbol": "OPEN_USDT",
            "tier": "HIGH_200",
            "opened_at": now - timedelta(hours=10),
            "entry_price": 100.0,
            "current_price": 90.0,
            "notional_usdt": 1000.0,
            "current_return_pct": 10.0,
            "best_profit_pct": 12.0,
            "mae_pct": 5.0,
            "mfe_pct": 12.0,
            "funding_pnl_usdt": 2.0,
            "entry_fee_usdt": 0.8,
            "exit_fee_usdt": 0.0,
            "entry_slippage_usdt": 2.5,
            "exit_slippage_usdt": 0.0,
            "exit_reason": None,
        },
        {
            "status": "closed",
            "slot_no": 2,
            "symbol": "CLOSED_USDT",
            "tier": "LOW_100",
            "opened_at": now - timedelta(days=1),
            "closed_at": now - timedelta(hours=2),
            "entry_price": 50.0,
            "current_price": 45.0,
            "notional_usdt": 500.0,
            "current_return_pct": 10.0,
            "best_profit_pct": 11.0,
            "mae_pct": 8.0,
            "mfe_pct": 12.0,
            "funding_pnl_usdt": -5.0,
            "entry_fee_usdt": 0.4,
            "exit_fee_usdt": 0.36,
            "entry_slippage_usdt": 1.25,
            "exit_slippage_usdt": 1.125,
            "net_pnl_usdt": 48.515,
            "exit_reason": "trail_gap1",
        },
    ]
    signals = [
        {
            "p2_at": now - timedelta(days=1),
            "stage_no": 1,
            "eligible_stage2": False,
            "trade_decision": None,
        },
        {
            "p2_at": now - timedelta(hours=10),
            "stage_no": 2,
            "eligible_stage2": True,
            "trade_decision": "accepted",
        },
        {
            "p2_at": now - timedelta(hours=1),
            "stage_no": 2,
            "eligible_stage2": True,
            "trade_decision": "ignored_capacity",
        },
    ]
    snapshots = [
        {"snapshot_at": now - timedelta(days=2), "equity_usdt": 2000.0, "gross_exposure_pct": 0.0},
        {"snapshot_at": now - timedelta(days=1), "equity_usdt": 2200.0, "gross_exposure_pct": 100.0},
        {"snapshot_at": now, "equity_usdt": 1980.0, "gross_exposure_pct": 50.0},
    ]

    s = build_t100_performance_summary(
        runtime=runtime,
        positions=positions,
        signals=signals,
        snapshots=snapshots,
        now_utc=now,
        timezone_name="Europe/Zurich",
    )

    assert s.current_equity_usdt == pytest.approx(2090.0)
    assert s.unrealized_pnl_usdt == pytest.approx(100.0)
    assert s.total_return_pct == pytest.approx(4.5)
    assert s.close_max_drawdown_pct == pytest.approx(-10.0)
    assert s.closed_wins == 1
    assert s.closed_losses == 0
    assert s.funding_net_usdt == pytest.approx(-3.0)
    assert s.fees_usdt == pytest.approx(1.56)
    assert s.slippage_usdt == pytest.approx(4.875)
    assert s.low_entries == 1
    assert s.high_entries == 1
    assert s.eligible_stage2 == 2
    assert s.accepted_stage2 == 1
    assert s.ignored_capacity == 1
    assert s.trail_exits == 1
    assert s.open_positions == 1
    assert s.open_lines[0].startswith("#1 OPEN_USDT")


def test_t100_dashboard_has_no_fake_drawdown_without_equity_curve():
    now = datetime(2026, 10, 7, 16, 0, tzinfo=UTC)
    s = build_t100_performance_summary(
        runtime={
            "starting_equity_usdt": 2000.0,
            "realized_equity_usdt": 2000.0,
            "started_at": now,
        },
        positions=[],
        signals=[],
        snapshots=[],
        now_utc=now,
        timezone_name="Europe/Zurich",
    )
    assert s.close_max_drawdown_pct is None
    assert s.closed_win_rate is None
