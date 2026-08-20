from __future__ import annotations

from datetime import UTC, datetime, timedelta

from app.research_analytics import (
    build_research_analytics,
    research_feature_lift_csv,
    research_signal_dataset_csv,
)


def _row(
    idx: int,
    *,
    volume_z: float,
    return_7d: float,
    target_20: bool,
    risk: str = "standard",
) -> dict:
    confirmed = datetime(2026, 8, 1, tzinfo=UTC) + timedelta(hours=idx)
    target20_at = confirmed + timedelta(hours=8 + idx) if target_20 else None
    feature = {
        "return_24h": 0.20 + idx * 0.01,
        "return_72h": 0.40 + idx * 0.02,
        "residual_return_24h": 0.15 + idx * 0.01,
        "cross_section_percentile": 0.90 + idx * 0.01,
        "volume_zscore_15m": volume_z,
        "distance_above_ema20_atr_4h": 1.0 + idx,
        "amount_24h": 5_000_000 + idx * 1_000_000,
        "spread_pct": 0.1 + idx * 0.01,
        "funding_rate": 0.0001 * idx,
        "fair_index_premium_pct": 0.1 * idx,
        "hold_vol": 1_000_000 + idx,
        "upper_wick_ratio_15m": 0.3 + idx * 0.02,
        "close_location_15m": 0.5 - idx * 0.02,
        "momentum_1h": 0.05 - idx * 0.01,
        "previous_momentum_1h": 0.08 - idx * 0.005,
        "momentum_decelerating": idx % 2 == 0,
        "below_ema9_15m": idx >= 3,
        "lower_high_and_close": idx >= 2,
        "structural_break_15m": idx >= 3,
    }
    row = {
        "episode_id": idx,
        "symbol": f"COIN{idx}_USDT",
        "risk_tier": risk,
        "confirmed_at": confirmed,
        "entry_price": 1.0,
        "run_score": 3 + (idx % 3),
        "exhaustion_score": 2 + (idx % 4),
        "hours_run_to_breakdown": 4.0 + idx,
        "hours_breakdown_to_retest": 0.5 + idx * 0.2,
        "hours_breakdown_to_confirmation": 1.0 + idx * 0.2,
        "hours_episode_to_confirmation": 8.0 + idx,
        "feature_snapshot": feature,
        "return_24h_pct": return_7d / 4,
        "return_48h_pct": return_7d / 3,
        "return_72h_pct": return_7d / 2,
        "return_168h_pct": return_7d,
        "target_20_at": target20_at,
        "path_rows": 672,
        "path_last_at": confirmed + timedelta(hours=168),
        "path_mfe_7d": max(0.25 if target_20 else 0.12, return_7d),
        "path_mae_7d": -(0.04 + idx * 0.01),
        "path_mfe_at": confirmed + timedelta(hours=24),
        "path_mae_at": confirmed + timedelta(hours=3),
    }
    for pct in (5, 10, 15, 20, 25, 30, 40):
        hit = target_20 or pct <= 10
        row[f"target_{pct}_at"] = confirmed + timedelta(hours=pct / 5 + idx) if hit else None
    row["target_20_path_at"] = (confirmed + timedelta(hours=4 + idx)) if target20_at is not None else None
    return row


def test_research_analytics_builds_baseline_target_sweep_and_feature_lift():
    rows = [
        _row(1, volume_z=0.5, return_7d=-0.20, target_20=False),
        _row(2, volume_z=0.8, return_7d=-0.10, target_20=False),
        _row(3, volume_z=1.2, return_7d=0.05, target_20=False),
        _row(4, volume_z=2.0, return_7d=0.20, target_20=True),
        _row(5, volume_z=2.5, return_7d=0.30, target_20=True),
        _row(6, volume_z=3.0, return_7d=0.40, target_20=True, risk="high_risk"),
    ]
    report = build_research_analytics(rows, generated_at=datetime(2026, 8, 20, tzinfo=UTC))

    assert report.baseline.total_signals == 6
    assert report.baseline.matured_7d == 6
    assert report.baseline.complete_paths_7d == 6
    assert report.baseline.target_20_rate_7d == 0.5
    assert report.baseline.positive_7d_rate == 4 / 6

    target20 = next(item for item in report.target_sweep if item.target_pct == 20)
    assert target20.sample == 6
    assert target20.hits == 3
    assert target20.hit_rate == 0.5

    volume_high = next(
        item
        for item in report.feature_slices
        if item.feature == "volume_zscore_15m" and item.bucket.startswith("HIGH")
    )
    assert volume_high.target_lift_pp is not None
    assert volume_high.target_lift_pp > 0


def test_research_csv_exports_include_flat_features_and_lifts():
    rows = [
        _row(1, volume_z=1.0, return_7d=-0.1, target_20=False),
        _row(2, volume_z=2.0, return_7d=0.2, target_20=True),
        _row(3, volume_z=3.0, return_7d=0.3, target_20=True),
    ]
    report = build_research_analytics(rows, generated_at=datetime(2026, 8, 20, tzinfo=UTC))
    lift = research_feature_lift_csv(report).decode()
    dataset = research_signal_dataset_csv(rows).decode()

    assert "target_lift_pp" in lift
    assert "volume_zscore_15m" in lift
    assert "hours_breakdown_to_retest" in dataset
    assert "target_40_at" in dataset
    assert "COIN1_USDT" in dataset
