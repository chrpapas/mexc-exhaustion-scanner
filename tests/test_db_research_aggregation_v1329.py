from datetime import UTC, datetime, timedelta

from app.research_path_aggregation import _aggregate_research_path_metrics


def _row(at, close_ret, favorable, adverse):
    return {
        "episode_id": 1,
        "candle_close_at": at,
        "close_return_pct": close_ret,
        "favorable_return_pct": favorable,
        "adverse_return_pct": adverse,
    }


def test_research_path_aggregation_matches_strategy_semantics():
    confirmed = datetime(2026, 8, 1, 0, 0, tzinfo=UTC)
    rows = [
        _row(confirmed + timedelta(minutes=15), 0.01, 0.02, -0.03),
        _row(confirmed + timedelta(hours=1), -0.04, 0.03, -0.12),
        _row(confirmed + timedelta(hours=2), 0.04, 0.06, -0.05),  # TP5 candle
        _row(confirmed + timedelta(hours=24), 0.07, 0.10, -0.20),
        _row(confirmed + timedelta(hours=168), -0.25, 0.25, -0.50),  # TP20 by 7d
        _row(confirmed + timedelta(hours=200), 0.11, 0.30, -0.10),
        _row(confirmed + timedelta(hours=340), 0.02, 0.40, -0.75),  # outside 14d
    ]

    m = _aggregate_research_path_metrics(confirmed, rows)

    assert m["path_rows"] == 7
    assert m["path_rows_7d"] == 5
    assert m["path_rows_14d"] == 6
    assert m["target_5_at"] == confirmed + timedelta(hours=2)
    assert m["target_20_path_at"] == confirmed + timedelta(hours=168)
    assert m["path_mae_before_target_5"] == -0.12
    assert m["path_mae_before_target_5_at"] == confirmed + timedelta(hours=1)
    assert m["path_mae_before_target_20"] == -0.50
    assert m["path_return_24h"] == 0.07
    assert m["path_return_168h"] == -0.25
    assert m["path_return_336h"] == 0.11
    assert m["path_latest_return"] == 0.02
    assert m["path_mfe_7d"] == 0.25
    assert m["path_mae_7d"] == -0.50
    assert m["path_mfe_14d"] == 0.30
    assert m["path_mae_14d"] == -0.50
    assert m["adverse_75_at"] == confirmed + timedelta(hours=340)


def test_research_path_aggregation_empty_preserves_null_summary():
    confirmed = datetime(2026, 8, 1, tzinfo=UTC)
    m = _aggregate_research_path_metrics(confirmed, [])
    assert m["path_rows"] is None
    assert m["target_5_at"] is None
    assert m["path_latest_return"] is None

from app.research_path_aggregation import _aggregate_performance_path_metrics


def test_performance_path_aggregation_supports_ledger_and_report_fields():
    confirmed = datetime(2026, 8, 1, 0, 0, tzinfo=UTC)
    rows = [
        _row(confirmed + timedelta(minutes=15), -0.02, 0.01, -0.04),
        _row(confirmed + timedelta(hours=1), -0.10, 0.03, -0.12),
        _row(confirmed + timedelta(hours=2), 0.05, 0.06, -0.08),
        _row(confirmed + timedelta(hours=24), 0.08, 0.21, -0.55),
        _row(confirmed + timedelta(hours=200), -0.30, 0.30, -1.20),
        _row(confirmed + timedelta(hours=260), 0.02, 0.40, -2.10),
    ]
    m = _aggregate_performance_path_metrics(confirmed, rows)

    assert m["target_5_at"] == confirmed + timedelta(hours=2)
    assert m["target_20_path_at"] == confirmed + timedelta(hours=24)
    assert m["path_mae_before_target_5"] == -0.12
    assert m["path_mae_before_target_20"] == -0.12
    assert m["adverse_50_at"] == confirmed + timedelta(hours=24)
    assert m["adverse_100_at"] == confirmed + timedelta(hours=200)
    assert m["adverse_200_path_at"] == confirmed + timedelta(hours=260)
    assert m["path_rows_10d"] == 5
    assert m["path_return_24h"] == 0.08
    assert m["path_return_240h"] == -0.30
    assert m["path_times"] == sorted(m["path_times"])
    assert m["path_returns"][-1] == 0.02
