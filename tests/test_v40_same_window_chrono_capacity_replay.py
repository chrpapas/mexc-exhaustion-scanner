"""No-exchange chronological V40 quota, pending stop and stress tests."""
from __future__ import annotations

from dataclasses import replace
from decimal import Decimal

import pytest

from scripts.v40_same_window_chrono_capacity_replay import (
    Holding, _fill_entry_notional, comparison, run_chronological,
)
from scripts.v40_same_window_pg_replay import BAR, ReplayDataError
from tests.test_v40_same_window_pg_replay import sample


def test_flat_one_entry_preserves_frozen_25pct_and_cost():
    snap=sample()
    result=run_chronological(snap,cost_bps=100,mode="REFERENCE")
    assert result["status"].startswith("ENHANCED_CHRONOLOGICAL")
    assert result["counts"]["entered"]==1
    assert result["open_positions"]==1
    assert result["closed_trades"]==0
    assert result["final_equity_usdt"]==1994.6
    assert result["peak_open_slots"]==1
    assert result["live_authorization"] is False


def test_30pct_vol_skip_is_applied_to_causal_49_prior_bars():
    snap=sample(alternating=True)
    x=run_chronological(snap,cost_bps=100,mode="REFERENCE")
    assert x["counts"]["v40_SKIP_vol_ge_30"]==1
    assert x["counts"].get("entered",0)==0
    assert x["return_pct"]==0


def test_stop_can_close_from_available_shared_prior_candle_turnover():
    snap=sample(stop_gap=True)
    x=run_chronological(snap,cost_bps=100,mode="REFERENCE")
    assert x["counts"]["entered"]==1
    assert x["counts"]["trigger_sl75"]==1
    assert x["counts"]["normal_exit_fills"]==1
    assert x["counts"].get("delayed_exit_fills",0)==0
    assert x["closed_trades"]==1
    assert x["open_positions"]==0


def test_entry_consumes_quota_causing_partial_trigger_and_delayed_gap_buyback():
    snap=sample(stop_gap=True)
    first=snap["signals"][0]["t"]
    for bar in snap["candles"]:
        if bar["t"] == first-BAR:
            bar["amount"]=10000.
        if bar["t"] == first+BAR:
            bar.update(open=200.,high=205.,low=195.,close=200.)
    x=run_chronological(snap,cost_bps=100,mode="REFERENCE")
    assert x["counts"]["entered"]==1
    assert x["counts"]["trigger_sl75"]==1
    assert x["counts"]["trigger_no_capacity_or_lot"]==1
    assert x["counts"]["delayed_exit_fills"]==1
    assert x["pending_mandatory_exits"]==0
    assert x["open_positions"]==0
    assert [f["kind"] for f in x["fills"]]==[
        "entry","delayed_exit",
    ]
    assert x["fills"][-1]["price"]==200.
    assert x["return_pct"] < -20


def test_missing_required_active_bar_halts_without_performance_claim():
    snap=sample()
    first=snap["signals"][0]["t"]
    snap["candles"]=[
        bar for bar in snap["candles"] if bar["t"]!=first
    ]
    x=run_chronological(snap,cost_bps=100,mode="REFERENCE")
    assert x["counts"]["missing_entry_bar_open"]==1
    assert x["counts"].get("entered",0)==0


def test_missing_bar_after_entry_halts_without_stale_mark():
    snap=sample()
    first=snap["signals"][0]["t"]
    snap["candles"]=[
        bar for bar in snap["candles"] if bar["t"]!=first+BAR
    ]
    x=run_chronological(snap,cost_bps=100,mode="REFERENCE")
    assert x["status"]=="INCOMPLETE_REQUIRED_BAR_OR_FINAL_MARK_NO_RETURN"
    assert x.get("return_pct") is None


def test_missing_just_entered_terminal_ticker_blocks_assumed_flat_mark():
    snap=sample()
    first=snap["signals"][0]["t"]
    snap["end"]=first+120
    x=run_chronological(snap,cost_bps=100,mode="REFERENCE")
    assert x["status"]=="INCOMPLETE_REQUIRED_BAR_OR_FINAL_MARK_NO_RETURN"


def test_current_known_lot_exit_does_not_leave_below_minimum_dust():
    p=Holding(
        symbol="EXAMPLE_USDT",signal_id=1,started=0,entry=100.,
        initial=100.,remaining=100.,mark=100.,
        lot={
            "size":Decimal("0.01"),
            "step":Decimal("1"),
            "minimum":Decimal("5"),
        },
    )
    # 100 contracts held, min five; cannot sell 97 and leave three.
    amount=_fill_entry_notional(p,97.,100.)
    assert amount==pytest.approx(95.)
    # Full close is allowed only with enough independent capacity.
    amount=_fill_entry_notional(p,100.,100.)
    assert amount==pytest.approx(100.)


def test_costs_are_charged_on_entry_and_each_exit_fill():
    snap=sample(stop_gap=True)
    x=run_chronological(snap,cost_bps=100,mode="REFERENCE")
    opening=x["fills"][0]["filled_usdt"]
    close=x["fills"][1]["buyback_usdt"]
    assert x["fees_usdt"]==pytest.approx((opening+close)*.0008)
    assert x["slippage_usdt"]==pytest.approx((opening+close)*.01)


def test_risk_trim_reduces_two_high_exposures_after_large_pre_stop_adverse_move():
    snap=sample(atr=5.8)
    first=snap["signals"][0]["t"]
    rows=[]
    for row in snap["candles"]:
        copy=dict(row)
        if row["t"]==first:
            copy.update(open=100.,high=170.,low=100.,close=170.)
        elif row["t"]>first:
            copy.update(open=170.,high=170.,low=170.,close=170.)
        copy["symbol"]="SECOND_USDT"
        rows.append(copy)
    snap["candles"]+=rows
    other=dict(snap["signals"][0],id=43,symbol="SECOND_USDT")
    snap["signals"].append(other)
    x=run_chronological(snap,cost_bps=100,mode="REFERENCE")
    assert x["counts"]["entered"]==2
    assert x["counts"]["trim_fills"]>=1
    assert x["fees_usdt"]>2
    assert x["peak_open_slots"]==2


def test_comparison_includes_six_strictly_uncertified_scenarios():
    out=comparison(sample(stop_gap=True),capital=2000.)
    assert len(out["scenarios"])==6
    assert out["real_money_execution_authorized"] is False
    for item in out["scenarios"]:
        x=item["enhanced_chronological"]
        assert x["live_authorization"] is False
        assert x["original_archived_simulator_equivalence_certified"] is False
        assert item["delta_enhanced_minus_min30_pct_points"] is not None


def test_invalid_snapshot_ohlc_rejected_without_trading():
    snap=sample()
    snap["candles"][0]["close"]=999.
    with pytest.raises(ReplayDataError,match="INVALID_MIN30_CANDLE"):
        run_chronological(snap,cost_bps=100,mode="REFERENCE")
