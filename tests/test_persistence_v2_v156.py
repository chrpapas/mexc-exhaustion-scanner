from __future__ import annotations

from datetime import timedelta
from pathlib import Path

from app.daily_bull_persistence_strategy import (
    DAILY_BULL_PERSISTENCE_V2_FREEZE_AT,
    DAILY_CORE_PERSISTENCE_SKIP_STRATEGY_V2,
    daily_bull_persistence_v2_state,
    mature_run_weak_breakdown_v1_state,
)
from app.research_analytics import build_current_strategy_research
from tests.test_daily_bull_persistence_v151 import _row


def _mature_shape(*, lower_high: bool = False, structural: bool = False, previous_momentum: float = 0.02):
    row = _row(990, persistence=False, target=False)
    row["hours_run_to_breakdown"] = 76.0
    row["feature_snapshot"] = dict(row["feature_snapshot"])
    row["feature_snapshot"].update({
        "previous_momentum_1h": previous_momentum,
        "lower_high_and_close": lower_high,
        "structural_break_15m": structural,
    })
    features = dict(row["feature_snapshot"])
    features["run_score"] = row["run_score"]
    features["hours_run_to_breakdown"] = row["hours_run_to_breakdown"]
    return row, features


def test_v2_catches_soph_like_mature_weak_breakdown_but_requires_both_missing_structures():
    _, soph = _mature_shape()
    assert mature_run_weak_breakdown_v1_state(soph) is True
    assert daily_bull_persistence_v2_state(soph) is True

    _, has_lower_high = _mature_shape(lower_high=True)
    assert mature_run_weak_breakdown_v1_state(has_lower_high) is False
    assert daily_bull_persistence_v2_state(has_lower_high) is False

    _, has_structural_break = _mature_shape(structural=True)
    assert mature_run_weak_breakdown_v1_state(has_structural_break) is False
    assert daily_bull_persistence_v2_state(has_structural_break) is False


def test_v2_mature_branch_requires_fresh_positive_previous_momentum_and_24h_age():
    _, negative_momentum = _mature_shape(previous_momentum=-0.01)
    assert daily_bull_persistence_v2_state(negative_momentum) is False

    row, young = _mature_shape()
    young["hours_run_to_breakdown"] = 23.99
    assert daily_bull_persistence_v2_state(young) is False


def test_v2_preserves_frozen_v1_early_branch_exactly():
    early = _row(991, persistence=True, target=False)
    features = dict(early["feature_snapshot"])
    features["run_score"] = early["run_score"]
    features["hours_run_to_breakdown"] = early["hours_run_to_breakdown"]
    assert daily_bull_persistence_v2_state(features) is True


def test_current_research_v2_tracks_mature_branch_and_clean_forward_freeze():
    mature, _ = _mature_shape()
    winner = _row(992, persistence=False, target=True)
    mature["confirmed_at"] = DAILY_BULL_PERSISTENCE_V2_FREEZE_AT + timedelta(minutes=1)
    winner["confirmed_at"] = DAILY_BULL_PERSISTENCE_V2_FREEZE_AT + timedelta(minutes=2)
    winner["target_5_at"] = winner["confirmed_at"] + timedelta(hours=1)

    report = build_current_strategy_research(
        [mature, winner], generated_at=DAILY_BULL_PERSISTENCE_V2_FREEZE_AT + timedelta(days=1)
    )
    assert report.strategy == DAILY_CORE_PERSISTENCE_SKIP_STRATEGY_V2
    assert report.persistence_flagged == 1
    assert report.admitted_signals == 1
    assert report.prospective_total_signals == 2
    assert report.prospective_persistence_flagged == 1
    assert report.prospective_admitted_signals == 1


def test_v2_decision_migration_allows_distinct_mature_skip_reason():
    sql = Path("migrations/020_persistence_v2_trader_decisions.sql").read_text()
    assert "ignored_mature_run_weak_breakdown_filter" in sql
    assert "ignored_daily_bull_persistence_filter" in sql
