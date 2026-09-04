from __future__ import annotations

from datetime import UTC, datetime, timedelta

from app.daily_regime import (
    DAILY_REGIME_V1_VERSION,
    daily_regime_snapshot_metadata,
    daily_regime_state,
    reconstruct_daily_regime_features,
)
from app.research_analytics import build_research_analytics
from tests.test_research_analytics_v128 import _base_row


def _daily_rows(start: datetime, closes: list[float]) -> list[dict]:
    rows = []
    for index, close in enumerate(closes):
        rows.append({
            "open_time": start + timedelta(days=index),
            "high": close * 1.02,
            "low": close * 0.98,
            "close": close,
        })
    return rows


def test_daily_regime_reconstruction_uses_completed_day1_candles_only():
    start = datetime(2026, 7, 1, tzinfo=UTC)
    completed_closes = [100.0 + i for i in range(30)]
    rows = _daily_rows(start, completed_closes)
    # This candle is still open at confirmation and must not contaminate the state.
    rows += _daily_rows(start + timedelta(days=30), [50.0])
    confirmed_at = start + timedelta(days=30, hours=12)

    values, sources = reconstruct_daily_regime_features(
        confirmed_at=confirmed_at,
        entry_price=132.0,
        day1_rows=rows,
    )
    assert values["daily_last_completed_close"] == completed_closes[-1]
    assert values["daily_close_above_ema20"] is True
    assert values["daily_ema20_slope"] > 0
    assert values["daily_momentum_3d"] > 0
    assert values["daily_momentum_7d"] > 0
    assert values["daily_distance_above_ema20_atr"] > 0
    assert sources["daily_momentum_3d"] == "day1_reconstruction"
    snapshot = dict(values)
    snapshot.update(daily_regime_snapshot_metadata(snapshot))
    assert snapshot["daily_regime_v1_version"] == DAILY_REGIME_V1_VERSION
    assert snapshot["daily_regime_v1_computable"] is True
    assert daily_regime_state(snapshot) is True


def test_daily_regime_state_is_structural_and_tristate():
    bullish = {
        "daily_close_above_ema20": True,
        "daily_ema20_slope": 0.01,
        "daily_momentum_3d": 0.03,
    }
    assert daily_regime_state(bullish) is True
    assert daily_regime_state({**bullish, "daily_momentum_3d": -0.01}) is False
    incomplete = dict(bullish)
    del incomplete["daily_ema20_slope"]
    assert daily_regime_state(incomplete) is None


def test_core_by_daily_regime_matrix_keeps_four_distinct_cells():
    rows = []
    specs = [
        (1, False, False, "target"),
        (2, False, True, "target"),
        (3, True, False, "target"),
        (4, True, True, "stop"),
    ]
    for episode_id, core, daily_bull, outcome in specs:
        row = _base_row(episode_id, "high_risk")
        row["feature_snapshot"] = dict(row["feature_snapshot"])
        row["feature_snapshot"]["distance_above_ema20_atr_4h"] = 3.0 if core else 2.0
        row["feature_snapshot"]["daily_close_above_ema20"] = daily_bull
        row["feature_snapshot"]["daily_ema20_slope"] = 0.01 if daily_bull else -0.01
        row["feature_snapshot"]["daily_momentum_3d"] = 0.03 if daily_bull else -0.02
        if outcome == "target":
            row["target_5_at"] = row["confirmed_at"] + timedelta(hours=1)
        else:
            row["adverse_75_at"] = row["confirmed_at"] + timedelta(hours=2)
        rows.append(row)

    report = build_research_analytics(
        rows,
        generated_at=max(row["confirmed_at"] for row in rows) + timedelta(days=1),
    )
    v = report.volatility
    assert v.daily_regime_computable_signals == 4
    assert v.daily_regime_missing_signals == 0
    assert v.daily_regime_bullish_signals == 2
    assert v.daily_regime_nonbullish_signals == 2
    cells = {cell.key: cell.validation for cell in v.daily_core_matrix}
    assert all(item.sample == 1 for item in cells.values())
    assert cells["core1_daily1"].stop_exits == 1
    assert cells["core1_daily0"].target_exits == 1
