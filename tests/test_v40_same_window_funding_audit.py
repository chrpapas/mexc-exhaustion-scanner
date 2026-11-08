"""Offline independent funding attribution tests, with no database/API."""
from __future__ import annotations

from copy import deepcopy

import pytest

from scripts.v40_same_window_funding_audit import verify_source_funding
from scripts.v40_same_window_chrono_capacity_replay import run_chronological
from scripts.v40_same_window_pg_replay import BAR, ReplayDataError
from tests.test_v40_same_window_pg_replay import sample


def funding_case(rate=0.001):
    source=sample()
    start=source["signals"][0]["t"]
    for bar in source["candles"]:
        if bar["t"]==start+BAR:
            bar.update(open=180.,high=180.,low=180.,close=180.)
    source["funding"]=[{
        "symbol":"SIREN_USDT",
        "t":start+BAR,
        "rate":rate,
    }]
    sim=run_chronological(source,cost_bps=100,mode="REFERENCE")
    assert sim["closed_trades"]==1
    assert sim["counts"]["funding_events"]==1
    return source,{
        "cost_bps_per_side":100,"lot_mode":"REFERENCE",
        "enhanced_chronological":sim,
    }


def test_independently_recompute_funding_from_prior_completed_mark_and_event():
    source,case=funding_case()
    out=verify_source_funding(source,case)
    assert out["status"]=="V40_EXPORTED_SOURCE_FUNDING_CASHFLOW_REBUILT_NONCERTIFYING"
    assert out["funding_cash_usdt_recomputed"]==pytest.approx(.5,abs=.001)
    assert out["funding_cash_usdt_reported"]==pytest.approx(.5,abs=.001)
    assert out["funding_events_recomputed"]==1
    assert out["source_rates_replayed_independently"] is True
    assert out["funding_source_completeness_verified"] is False


def test_negative_funding_rates_are_replayed_signed():
    source,case=funding_case(rate=-.002)
    out=verify_source_funding(source,case)
    assert out["funding_cash_usdt_recomputed"]==pytest.approx(-1.0,abs=.001)


def test_no_funding_event_is_not_assumed_present():
    source=sample(stop_gap=True)
    case={
        "cost_bps_per_side":100,"lot_mode":"REFERENCE",
        "enhanced_chronological":run_chronological(
            source,cost_bps=100,mode="REFERENCE",
        ),
    }
    out=verify_source_funding(source,case)
    assert out["funding_events_recomputed"]==0
    assert out["funding_cash_usdt_recomputed"]==0


def test_tampered_historical_funding_rate_is_detected():
    source,case=funding_case()
    source["funding"][0]["rate"]=0.005
    with pytest.raises(ReplayDataError,match="FUNDING_AUDIT_TRADE_FUNDING_MISMATCH"):
        verify_source_funding(source,case)


def test_tampered_historical_prior_mark_is_detected():
    source,case=funding_case()
    start=source["signals"][0]["t"]
    for bar in source["candles"]:
        if bar["t"]==start:
            bar["close"]=110.
    with pytest.raises(ReplayDataError,match="FUNDING_AUDIT_TRADE_FUNDING_MISMATCH"):
        verify_source_funding(source,case)


def test_duplicate_rate_events_are_rejected():
    source,case=funding_case()
    source["funding"].append(deepcopy(source["funding"][0]))
    with pytest.raises(ReplayDataError,match="FUNDING_AUDIT_DUPLICATE_RATE"):
        verify_source_funding(source,case)


def test_open_position_not_claimed_funding_complete():
    source=sample()
    case={
        "cost_bps_per_side":100,"lot_mode":"REFERENCE",
        "enhanced_chronological":run_chronological(
            source,cost_bps=100,mode="REFERENCE",
        ),
    }
    out=verify_source_funding(source,case)
    assert out["status"]=="OPEN_POSITION_FUNDING_NOT_FINALIZED"


def test_forged_funding_cash_report_is_detected():
    source,case=funding_case()
    case["enhanced_chronological"]["funding_usdt"]+=10
    with pytest.raises(ReplayDataError,match="FUNDING_AUDIT_AGGREGATE_MISMATCH"):
        verify_source_funding(source,case)


def test_funding_on_entry_boundary_is_not_credited():
    source=sample(stop_gap=True)
    first=source["signals"][0]["t"]
    source["funding"]=[{
        "symbol":"SIREN_USDT","t":first,"rate":.03,
    }]
    sim=run_chronological(source,cost_bps=100,mode="REFERENCE")
    assert sim["counts"].get("funding_events",0)==0
    out=verify_source_funding(source,{
        "cost_bps_per_side":100,"lot_mode":"REFERENCE",
        "enhanced_chronological":sim,
    })
    assert out["funding_cash_usdt_recomputed"]==0
