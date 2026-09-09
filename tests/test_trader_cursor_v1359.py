from app.trader_db import paper_run_reset_cursor


def test_new_paper_run_preserves_previous_cursor_by_default():
    runtime = {"last_signal_id": 818, "active_run_id": "old_run"}
    assert paper_run_reset_cursor(runtime, process_existing=False) == 818


def test_explicit_process_existing_rewinds_cursor():
    runtime = {"last_signal_id": 818, "active_run_id": "old_run"}
    assert paper_run_reset_cursor(runtime, process_existing=True) == 0


def test_missing_cursor_defaults_to_zero():
    assert paper_run_reset_cursor({}, process_existing=False) == 0
