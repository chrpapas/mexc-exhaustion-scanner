"""Offline short native-stop trailing math; no network."""
from decimal import Decimal as D
from app.t100_live_trailing import next_short_stop

def test_no_trail_before_arm():
    x=next_short_stop(entry=D("100"),low=D("95"),current_stop=D("175"),
        best_profit_pct=D("0"),price_tick=D("0.01"),armed=False)
    assert not x.active and x.stop_price==D("175")

def test_arm_14_protect_13_point_5():
    x=next_short_stop(entry=D("100"),low=D("86"),current_stop=D("175"),
        best_profit_pct=D("0"),price_tick=D("0.01"),armed=False)
    assert x.active and x.stop_price==D("86.50")

def test_stop_never_moves_against_position():
    x=next_short_stop(entry=D("100"),low=D("96"),current_stop=D("70"),
        best_profit_pct=D("20"),price_tick=D("0.01"),armed=True)
    assert not x.changed and x.stop_price==D("70")

def test_monotonic_tightening():
    old=D("175")
    for low in ("86","80","90","65"):
        x=next_short_stop(entry=D("100"),low=D(low),current_stop=old,
            best_profit_pct=D("14"),price_tick=D("0.01"),armed=True)
        assert x.stop_price<=old
        old=x.stop_price
