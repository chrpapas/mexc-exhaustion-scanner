from __future__ import annotations

from app.research_analytics import (
    CONTINUATION_CORE_V1_BASE_POSITION_PCT,
    CONTINUATION_CORE_V1_CROSS_SECTION_MIN,
    CONTINUATION_CORE_V1_EMA_DISTANCE_ATR_MIN,
    CONTINUATION_CORE_V1_FLAGGED_POSITION_PCT,
    CONTINUATION_CORE_V1_PREVIOUS_MOMENTUM_MIN,
    CONTINUATION_CORE_V1_RUN_SCORE_MIN,
    _continuation_core_v1_position_fractions,
    _continuation_core_v1_state,
)


def _row(*, run_score=5, ema=3.0, prev=0.01, cross=0.98):
    return {
        "episode_id": 1,
        "run_score": run_score,
        "feature_snapshot": {
            "distance_above_ema20_atr_4h": ema,
            "previous_momentum_1h": prev,
            "cross_section_percentile": cross,
        },
    }


def test_continuation_core_v1_frozen_boundaries_and_or_branch():
    assert CONTINUATION_CORE_V1_RUN_SCORE_MIN == 5.0
    assert CONTINUATION_CORE_V1_EMA_DISTANCE_ATR_MIN == 3.0
    assert CONTINUATION_CORE_V1_CROSS_SECTION_MIN == 0.99
    assert CONTINUATION_CORE_V1_PREVIOUS_MOMENTUM_MIN == 0.0
    assert CONTINUATION_CORE_V1_FLAGGED_POSITION_PCT == 0.025
    assert CONTINUATION_CORE_V1_BASE_POSITION_PCT == 0.05

    assert _continuation_core_v1_state(_row(prev=0.01, cross=0.98)) is True
    assert _continuation_core_v1_state(_row(prev=-0.01, cross=0.99)) is True
    assert _continuation_core_v1_state(_row(prev=0.0, cross=0.9899)) is False
    assert _continuation_core_v1_state(_row(run_score=4.999, prev=0.10, cross=1.0)) is False
    assert _continuation_core_v1_state(_row(ema=2.999, prev=0.10, cross=1.0)) is False


def test_continuation_core_v1_missing_is_tristate_and_not_sized():
    row = _row()
    del row["feature_snapshot"]["previous_momentum_1h"]
    assert _continuation_core_v1_state(row) is None
    assert _continuation_core_v1_position_fractions([row]) == {}


def test_continuation_core_v1_position_sizing():
    flagged = _row(prev=0.1)
    safe = _row(prev=-0.1, cross=0.98)
    safe["episode_id"] = 2
    fractions = _continuation_core_v1_position_fractions([flagged, safe])
    assert fractions == {1: 0.025, 2: 0.05}
