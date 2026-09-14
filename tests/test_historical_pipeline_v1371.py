from datetime import UTC, datetime
from pathlib import Path

from app.historical_pipeline import PipelineState, _terminal_fetch_reason, _terminal_universe_reason
from app.historical_research import resolve_frozen_window


def test_frozen_window_is_reused(tmp_path: Path):
    start1, end1 = resolve_frozen_window(tmp_path, months=6)
    start2, end2 = resolve_frozen_window(tmp_path, months=6)
    assert start1 == start2
    assert end1 == end2
    assert (tmp_path / "research-window.json").exists()


def test_pipeline_terminal_reasons_are_conservative():
    assert _terminal_fetch_reason("complete")
    assert not _terminal_fetch_reason("runtime_budget_reached")
    assert _terminal_universe_reason("reached_window_start")
    assert _terminal_universe_reason("announcement_pages_exhausted")
    assert _terminal_universe_reason("max_pages_reached")
    assert not _terminal_universe_reason("throttling_guard")


def test_pipeline_state_defaults_to_current_candles():
    state = PipelineState()
    assert state.stage == "current_candles"
    assert not state.completed
