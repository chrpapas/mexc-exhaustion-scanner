from __future__ import annotations

from datetime import timedelta

from app.research_analytics import (
    DAILY_BULL_PERSISTENCE_V1_FREEZE_AT,
    DAILY_BULL_PERSISTENCE_V2_FREEZE_AT,
    build_current_strategy_research,
    research_current_strategy_csv,
)
from tests.test_daily_bull_persistence_v151 import _row


def test_v155_current_research_builder_only_replays_promoted_admission():
    vetoed = _row(901, persistence=True, target=False)
    allowed = _row(902, persistence=False, target=True)
    vetoed["confirmed_at"] = DAILY_BULL_PERSISTENCE_V1_FREEZE_AT + timedelta(minutes=1)
    allowed["confirmed_at"] = DAILY_BULL_PERSISTENCE_V1_FREEZE_AT + timedelta(minutes=2)
    allowed["target_5_at"] = allowed["confirmed_at"] + timedelta(hours=1)

    report = build_current_strategy_research(
        [vetoed, allowed],
        generated_at=DAILY_BULL_PERSISTENCE_V2_FREEZE_AT + timedelta(days=1),
    )

    assert report.total_signals == 2
    assert report.persistence_flagged == 1
    assert report.admitted_signals == 1
    assert report.admitted_validation.target_exits == 1
    assert report.portfolio.eligible_signals == 1
    assert report.portfolio.entered == 1
    assert report.prospective_total_signals == 0
    assert report.prospective_persistence_flagged == 0
    assert report.prospective_admitted_signals == 0


def test_v155_current_strategy_csv_has_only_retrospective_and_true_forward_rows():
    allowed = _row(903, persistence=False, target=True)
    allowed["confirmed_at"] = DAILY_BULL_PERSISTENCE_V2_FREEZE_AT + timedelta(minutes=1)
    allowed["target_5_at"] = allowed["confirmed_at"] + timedelta(hours=1)
    report = build_current_strategy_research(
        [allowed], generated_at=DAILY_BULL_PERSISTENCE_V2_FREEZE_AT + timedelta(days=1)
    )
    text = research_current_strategy_csv(report).decode("utf-8")
    lines = [line for line in text.splitlines() if line]
    assert len(lines) == 3
    assert "retrospective" in lines[1]
    assert "true_forward" in lines[2]
    assert "tp5_challenger" not in text
    assert "hold_7d" not in text
    assert "pcr" not in text.lower()
