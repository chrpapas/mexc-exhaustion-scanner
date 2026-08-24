from datetime import UTC, datetime, timedelta

from app.research_analytics import _portfolio_replay, build_research_analytics


def _row(ep: int, at: datetime, *, t2_h: float = 0.5, t5_h: float = 4.0):
    return {
        "episode_id": ep,
        "symbol": f"H{ep}_USDT",
        "risk_tier": "standard",
        "confirmed_at": at,
        "target_2_at": at + timedelta(hours=t2_h),
        "target_5_at": at + timedelta(hours=t5_h),
        "path_latest_return": 0.06,
        "path_mae_before_target_5": -0.03,
        "path_mae_before_target_5_at": at + timedelta(minutes=15),
    }


def test_hybrid_replay_can_mix_tp2_and_tp5_without_changing_six_slot_capacity():
    start = datetime(2026, 8, 1, tzinfo=UTC)
    rows = [_row(1, start), _row(2, start)]

    hybrid = _portfolio_replay(
        rows,
        strategy="tp5_challenger",
        generated_at=start + timedelta(days=1),
        target_pct_by_episode={1: 2, 2: 5},
        strategy_name_override="hybrid_test",
    )
    tp2 = _portfolio_replay(
        rows,
        strategy="tp2_challenger",
        generated_at=start + timedelta(days=1),
    )
    tp5 = _portfolio_replay(
        rows,
        strategy="tp5_challenger",
        generated_at=start + timedelta(days=1),
    )

    assert hybrid.strategy == "hybrid_test"
    assert hybrid.closed == 2
    assert hybrid.max_observed_exposure_pct == 0.10
    assert hybrid.median_holding_hours == 2.25
    assert tp2.marked_return < hybrid.marked_return < tp5.marked_return


def test_hybrid_target_map_defaults_unclassified_episode_to_tp5():
    start = datetime(2026, 8, 1, tzinfo=UTC)
    rows = [_row(1, start)]
    hybrid = _portfolio_replay(
        rows,
        strategy="tp5_challenger",
        generated_at=start + timedelta(days=1),
        target_pct_by_episode={},
        strategy_name_override="hybrid_default_tp5",
    )
    tp5 = _portfolio_replay(
        rows,
        strategy="tp5_challenger",
        generated_at=start + timedelta(days=1),
    )
    assert hybrid.median_holding_hours == tp5.median_holding_hours == 4.0
    assert hybrid.marked_return == tp5.marked_return


def test_build_report_exposes_both_hybrid_portfolios_and_keeps_insufficient_on_tp5():
    start = datetime(2026, 8, 1, tzinfo=UTC)
    rows = [_row(1, start), _row(2, start + timedelta(minutes=15))]
    report = build_research_analytics(
        rows,
        generated_at=start + timedelta(days=1),
        regime_history_rows=[],
    )
    hybrids = {item.strategy: item for item in report.hybrid_portfolios}
    assert set(hybrids) == {
        "hybrid1_ep_mix_tp5_regime_tp2",
        "hybrid2_ep_tp5_mix_regime_tp2",
    }
    assert report.token_regime.insufficient_signals == 2
    # With no reliable behaviour profile, both hybrid hypotheses deliberately
    # fall back to TP5 for every signal.
    for item in hybrids.values():
        assert item.entered == report.calendar_throughput.tp5.entered
        assert item.median_holding_hours == report.calendar_throughput.tp5.median_holding_hours
        assert item.marked_return == report.calendar_throughput.tp5.marked_return
