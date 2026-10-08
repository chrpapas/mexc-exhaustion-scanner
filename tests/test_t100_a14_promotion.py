import inspect

from app import t100_200_strategy as strategy
from app.t100_200_worker import T100Worker


def test_promoted_strategy_identity_and_exit_geometry():
    assert strategy.STRATEGY_ID == "t100_200_stage2_p15_a4_sl75_trail14_gap0p5_v2"
    assert strategy.PAPER_RUN_ID == "t100_200_stage2_p15_a4_sl75_trail14_gap0p5_shadow_v2"
    assert strategy.TRAIL_ACTIVATION_PCT == 14.0
    assert strategy.TRAIL_GAP_PCT == 0.5
    assert strategy.STOP_PCT == 75.0


def test_new_positions_explicitly_store_promoted_geometry():
    source = inspect.getsource(T100Worker._admit_pending_stage2)
    assert "trail_activation_pct,trail_gap_pct" in source
    assert "TRAIL_ACTIVATION_PCT, TRAIL_GAP_PCT" in source


def test_position_exit_reads_stored_geometry():
    source = inspect.getsource(T100Worker._process_position_bar)
    assert 'p["trail_activation_pct"]' in source
    assert 'p["trail_gap_pct"]' in source


def test_render_remains_paper_only():
    render = open("render.yaml", encoding="utf-8").read()
    assert "startCommand: python -m app.t100_200_trader" in render
    assert "TRADING_MODE\n        value: paper" in render
