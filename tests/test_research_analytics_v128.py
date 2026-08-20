from __future__ import annotations

from datetime import UTC, datetime, timedelta

from app.research_analytics import build_research_analytics, shadow_entry_scores


def _base_row(episode_id: int, risk_tier: str = "standard") -> dict:
    confirmed = datetime(2026, 8, 1, tzinfo=UTC) + timedelta(hours=episode_id)
    row = {
        "episode_id": episode_id,
        "symbol": f"C{episode_id}_USDT",
        "risk_tier": risk_tier,
        "confirmed_at": confirmed,
        "entry_price": 1.0,
        "run_score": 5,
        "exhaustion_score": 5,
        "hours_run_to_breakdown": 5.0,
        "hours_breakdown_to_retest": 0.25,
        "hours_breakdown_to_confirmation": 0.5,
        "hours_episode_to_confirmation": 5.5,
        "feature_snapshot": {
            "return_24h": 0.30,
            "return_72h": 0.70,
            "residual_return_24h": 0.25,
            "cross_section_percentile": 0.99,
            "volume_zscore_15m": -0.5,
            "distance_above_ema20_atr_4h": 2.0,
            "amount_24h": 20_000_000,
            "spread_pct": 0.05,
            "funding_rate": 0.0001,
            "fair_index_premium_pct": -0.1,
            "hold_vol": 1_000_000,
            "upper_wick_ratio_15m": 0.2,
            "close_location_15m": 0.3,
            "momentum_1h": -0.08,
            "previous_momentum_1h": 0.02,
            "momentum_decelerating": True,
            "below_ema9_15m": True,
            "lower_high_and_close": True,
            "structural_break_15m": True,
        },
        "return_24h_pct": 0.10,
        "return_48h_pct": 0.20,
        "return_72h_pct": 0.30,
        "return_168h_pct": 0.50,
        "path_last_at": confirmed + timedelta(hours=336),
        "path_mfe_7d": 0.60,
        "path_mae_7d": -0.20,
        "path_mfe_at": confirmed + timedelta(hours=120),
        "path_mae_at": confirmed + timedelta(hours=10),
        "path_mfe_14d": 0.80,
        "path_mae_14d": -0.25,
        "path_mfe_14d_at": confirmed + timedelta(hours=250),
        "path_mae_14d_at": confirmed + timedelta(hours=200),
    }
    for hours, value in {
        24: 0.10,
        48: 0.20,
        72: 0.30,
        96: 0.35,
        120: 0.40,
        144: 0.45,
        168: 0.50,
        192: 0.52,
        240: 0.60,
        288: 0.65,
        336: 0.70,
    }.items():
        row[f"path_return_{hours}h"] = value
    return row


def test_standard_exit_sweep_extends_through_14_days():
    row = _base_row(1)
    report = build_research_analytics([row], generated_at=datetime(2026, 8, 20, tzinfo=UTC))

    assert report.baseline.complete_paths_14d == 1
    day4 = next(item for item in report.standard_exit_sweep if item.horizon_hours == 96)
    day14 = next(item for item in report.standard_exit_sweep if item.horizon_hours == 336)
    assert day4.sample == 1
    assert day4.avg_return == 0.35
    assert day14.avg_return == 0.70
    assert abs(day14.avg_return_per_day - 0.05) < 1e-12


def test_high_risk_timeout_takes_tp20_before_timeout_otherwise_timeout_return():
    winner = _base_row(1, "high_risk")
    winner["target_20_at"] = winner["confirmed_at"] + timedelta(hours=10)
    loser = _base_row(2, "high_risk")
    loser["return_48h_pct"] = -0.10
    loser["path_return_48h"] = -0.10

    report = build_research_analytics([winner, loser], generated_at=datetime(2026, 8, 20, tzinfo=UTC))
    timeout48 = next(item for item in report.high_risk_timeout_sweep if item.timeout_hours == 48)

    assert timeout48.sample == 2
    assert timeout48.target_hits == 1
    assert timeout48.target_hit_rate == 0.5
    assert abs(timeout48.avg_strategy_return - 0.05) < 1e-12


def test_stop_survival_counts_eventual_winners_killed_before_target():
    a = _base_row(1)
    b = _base_row(2)
    for row, mae in ((a, -0.15), (b, -0.35)):
        row["target_20_at"] = row["confirmed_at"] + timedelta(hours=20)
        row["target_20_path_at"] = row["target_20_at"]
        row["path_mae_before_target_20"] = mae

    report = build_research_analytics([a, b], generated_at=datetime(2026, 8, 20, tzinfo=UTC))
    stop20 = next(item for item in report.stop_survival if item.risk_tier == "all" and item.stop_pct == 20)

    assert stop20.winners_with_path == 2
    assert stop20.winners_killed == 1
    assert stop20.kill_rate == 0.5


def test_shadow_scores_reward_fade_profile_and_flag_continuation_profile():
    fade = _base_row(1)
    quality, continuation = shadow_entry_scores(fade)
    assert quality >= 8
    assert continuation <= 2

    momentum = _base_row(2)
    momentum["run_score"] = 6
    momentum["exhaustion_score"] = 6
    momentum["hours_run_to_breakdown"] = 30
    momentum["hours_episode_to_confirmation"] = 31
    momentum["feature_snapshot"].update(
        {
            "volume_zscore_15m": 2.0,
            "funding_rate": 0.001,
            "distance_above_ema20_atr_4h": 4.0,
            "momentum_1h": -0.01,
        }
    )
    quality2, continuation2 = shadow_entry_scores(momentum)
    assert quality2 < quality
    assert continuation2 >= 8


def test_delayed_entry_summary_uses_only_complete_delayed_paths():
    raw = _base_row(1)
    delayed = [
        {
            "episode_id": 1,
            "risk_tier": "standard",
            "delay_minutes": 60,
            "entry_at": raw["confirmed_at"] + timedelta(hours=1),
            "path_complete_7d": True,
            "return_7d_pct": 0.40,
            "mfe_7d": 0.55,
            "mae_7d": -0.10,
            "target_20_at": raw["confirmed_at"] + timedelta(hours=25),
        },
        {
            "episode_id": 2,
            "risk_tier": "standard",
            "delay_minutes": 60,
            "entry_at": raw["confirmed_at"] + timedelta(hours=1),
            "path_complete_7d": False,
            "return_7d_pct": 0.80,
            "mfe_7d": 0.90,
            "mae_7d": -0.05,
            "target_20_at": raw["confirmed_at"] + timedelta(hours=5),
        },
    ]
    report = build_research_analytics(
        [raw], generated_at=datetime(2026, 8, 20, tzinfo=UTC), delayed_entry_rows=delayed
    )
    plus60 = next(item for item in report.delayed_entries if item.delay_minutes == 60)
    assert plus60.sample == 1
    assert plus60.avg_return_7d == 0.40
    assert plus60.median_adverse_7d == 0.10


def test_path_completeness_rejects_sparse_7d_path_even_with_late_last_candle():
    row = _base_row(1)
    row["path_rows_7d"] = 100
    report = build_research_analytics([row], generated_at=datetime(2026, 8, 20, tzinfo=UTC))
    assert report.baseline.complete_paths_7d == 0


def test_path_completeness_accepts_98pct_7d_coverage():
    row = _base_row(1)
    row["path_rows_7d"] = 659
    report = build_research_analytics([row], generated_at=datetime(2026, 8, 20, tzinfo=UTC))
    assert report.baseline.complete_paths_7d == 1
