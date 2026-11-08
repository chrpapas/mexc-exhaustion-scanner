"""Deterministic offline tests for live-Postgres V40 replay projection.

No Postgres connection, external API, exchange order or writes are made.
"""
from __future__ import annotations

from datetime import datetime,timezone
import pytest

from scripts.v40_same_window_pg_replay import (
    BAR, DEFAULT_END, DEFAULT_START, ReplayDataError,
    epoch, replay, run_scenario, lot_sizing,
)


def sample(*, alternating=False, stop_gap=False, atr=4.5):
    start=epoch(DEFAULT_START)
    first=((start+BAR-1)//BAR)*BAR
    end=first+5*BAR
    base=first-49*BAR
    bars=[]
    for i in range(55):
        t=base+i*BAR
        price=100.0 if not alternating else (80. if i%2 else 120.)
        if stop_gap and t==first:
            bars.append(dict(
                symbol="SIREN_USDT",t=t,open=185.,high=190.,
                low=185.,close=187.,amount=100000.,
            ))
        else:
            bars.append(dict(
                symbol="SIREN_USDT",t=t,open=price,high=price,
                low=price,close=price,amount=100000.,
            ))
    signal_price=bars[48]["close"]
    snap={
        "source":"SYNTHETIC_TEST_NO_DB",
        "start":start,"end":end,
        "signals":[{
            "id":42,"symbol":"SIREN_USDT","t":first,
            "price":signal_price,
            "atr7":atr,"r24":0.2,"strict365":True,"stage1":True,
            "tier":"LOW_100","paper_decision":"accepted",
        }],
        "candles":bars,
        "funding":[],
        "specs":{},
        "tickers":{},
        "paper_db_snapshot":None,
    }
    return snap


def test_same_window_low_tier_uses_v40_25pct_notional():
    snap=sample()
    out=run_scenario(snap,cost_bps=100,mode="REFERENCE")
    assert out["status"].startswith("INDICATIVE_V40")
    assert out["counts"]["eligible_stage2"]==1
    assert out["counts"]["v40_entered"]==1
    assert out["open_positions"]==1
    assert out["fees_usdt"]==0.4
    assert out["slippage_usdt"]==5.0
    assert out["final_equity_usdt"]==1994.6
    assert out["funding_coverage_not_certified"]


def test_high_tier_62p5pct_before_v40_bounds():
    snap=sample(atr=5.8)
    out=run_scenario(snap,cost_bps=100,mode="REFERENCE")
    assert out["counts"]["v40_entered"]==1
    assert out["fees_usdt"]==1.0
    assert out["slippage_usdt"]==12.5
    assert out["final_equity_usdt"]==1986.5


def test_v40_volatility_skip_is_a_rejection_not_loss():
    snap=sample(alternating=True)
    out=run_scenario(snap,cost_bps=100,mode="REFERENCE")
    assert out["counts"]["v40_SKIP_vol_ge_30"]==1
    assert out["counts"].get("v40_entered",0)==0
    assert out["final_equity_usdt"]==2000.0


def test_missing_causal_history_does_not_fake_a_trade():
    snap=sample()
    snap["candles"]=snap["candles"][1:]
    out=run_scenario(snap,cost_bps=100,mode="REFERENCE")
    assert out["counts"]["missing_causal_49_bars"]==1
    assert out["counts"].get("v40_entered",0)==0


def test_missing_in_position_bar_makes_performance_unavailable():
    snap=sample()
    first=((snap["start"]+BAR-1)//BAR)*BAR
    snap["candles"]=[b for b in snap["candles"] if b["t"] != first]
    out=run_scenario(snap,cost_bps=100,mode="REFERENCE")
    assert out["status"]=="INCOMPLETE_POSITION_CANDLE_HISTORY_NO_PERFORMANCE_CLAIM"
    assert out.get("return_pct") is None


def test_stop_gap_uses_worse_open_not_nominal_trigger():
    snap=sample(stop_gap=True)
    out=run_scenario(snap,cost_bps=100,mode="REFERENCE")
    assert out["counts"]["exits_sl75"]==1
    assert out["closed"][0]["exit"]==185.0
    assert out["open_positions"]==0
    assert out["return_pct"] < -20.


def test_contract_spec_modes_explicitly_do_not_invent_history():
    price=100
    known={
        "SIREN_USDT":{
            "first_seen":epoch(DEFAULT_START)-1000,
            "metadata":{
                "contractSize":"0.01", "volUnit":1,
                "minVol":1,"maxVol":20,
            },
        }
    }
    a,tag=lot_sizing("SIREN_USDT",epoch(DEFAULT_START),100,price,known,"HYBRID")
    assert a==100
    assert tag=="CURRENT_METADATA_LOT_PROXY"
    future={
        "SIREN_USDT":{
            "first_seen":epoch(DEFAULT_START)+1000,
            "metadata":known["SIREN_USDT"]["metadata"],
        }
    }
    b,label=lot_sizing("SIREN_USDT",epoch(DEFAULT_START),100,price,future,"KNOWN_ONLY")
    assert b==0
    assert label=="HISTORICAL_CONTRACT_SPEC_UNKNOWN"
    c,label=lot_sizing("SIREN_USDT",epoch(DEFAULT_START),100,price,future,"HYBRID")
    assert c==100
    assert label=="UNKNOWN_SPEC_REFERENCE_FALLBACK"


def test_six_scenarios_and_paper_benchmark_separated():
    results=replay(sample(),initial_equity=2000)
    assert len(results["scenarios"])==6
    assert results["paper_dashboard"]["return_pct"]==96.88
    assert results["status"]=="RESEARCH_PROXY_NOT_CERTIFIED_FULL_V40_BACKTEST"
    assert all(x["entry_allowed"] is False for x in results["scenarios"]
               if "entry_allowed" in x)


def test_naive_timestamp_and_inconsistent_ohlc_fail_closed():
    with pytest.raises(ReplayDataError):
        epoch("2026-11-07T18:05:00")
    snap=sample()
    snap["candles"][-1]["high"]=99
    with pytest.raises(ReplayDataError,match="OHLC_INCONSISTENCY"):
        run_scenario(snap,cost_bps=100,mode="REFERENCE")


def test_zero_volume_prior_bar_cannot_create_frozen_admission():
    snap=sample()
    snap["candles"][1]["amount"]=0
    out=run_scenario(snap,cost_bps=100,mode="REFERENCE")
    assert out["counts"].get("v40_entered",0)==0
    assert out["counts"]["v40_MISSING_causal_49_bar_history_incomplete"]==1
