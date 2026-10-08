import inspect

from app.t100_200_scanner import T100Scanner
from app.t100_200_trader import T100Trader


def test_scanner_process_eval_never_executes_portfolio():
    source = inspect.getsource(T100Scanner.process_eval)
    assert "_select_eval_candidates" in source
    assert "_feature_for_symbol" in source
    assert "include_recent_m30=True" in source
    assert "_process_symbol_row" in source
    assert "_build_eval_rows" not in source
    assert "_process_position_bar" not in source
    assert "_admit_pending_stage2" not in source


def test_trader_cycle_never_scans_universe_or_generates_signals():
    source = inspect.getsource(T100Trader.cycle)
    assert "_admit_pending_stage2" in source
    assert "_consume_one_eval" in source
    assert "refresh_contracts" not in source
    assert "sync_market_history" not in source
    assert "_build_eval_rows" not in source
    assert "_process_symbol_row" not in source


def test_render_roles_are_dedicated():
    render = open("render.yaml", encoding="utf-8").read()
    assert "startCommand: python -m app.t100_200_scanner" in render
    assert "startCommand: python -m app.t100_200_trader" in render
