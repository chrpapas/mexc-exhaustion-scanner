"""Independent chronological JSON ledger-audit tests (no DB or exchange)."""
from __future__ import annotations

from copy import deepcopy

import pytest

from scripts.v40_same_window_chrono_capacity_replay import (
    comparison, run_chronological,
)
from scripts.v40_same_window_chrono_trace_audit import audit_trace
from scripts.v40_same_window_pg_replay import BAR, ReplayDataError
from tests.test_v40_same_window_pg_replay import sample


def case(*, stop_gap=True, cost=100):
    snap=sample(stop_gap=stop_gap)
    result=run_chronological(snap,cost_bps=cost,mode="REFERENCE")
    return snap,{
        "cost_bps_per_side":cost,"lot_mode":"REFERENCE",
        "enhanced_chronological":result,
    }


def test_flat_stop_with_complete_fill_cash_conserves():
    snap, s=case()
    out=audit_trace(snap,s)
    assert out["status"]=="V40_TRACE_LEDGER_ARITHMETIC_CONSISTENT_NOT_EXECUTION_CERTIFIED"
    assert out["entries_checked"]==1
    assert out["exits_checked"]==1
    assert out["closed_trades_checked"]==1
    assert out["recomputed_final_usdt"]==s["enhanced_chronological"]["final_equity_usdt"]
    assert out["historical_execution_certified"] is False


def test_all_six_comparison_cases_are_auditable_when_flat():
    snap=sample(stop_gap=True)
    cases=comparison(snap)["scenarios"]
    assert len(cases)==6
    for item in cases:
        out=audit_trace(snap,item)
        assert out["status"].startswith("V40_TRACE_LEDGER_ARITHMETIC_CONSISTENT")


def test_open_position_cannot_be_claimed_cash_reconciled():
    snap,s=case(stop_gap=False)
    out=audit_trace(snap,s)
    assert out["status"]=="NO_FULL_CASH_AUDIT_OPEN_POSITION_MARKS_NOT_IN_LEDGER"


@pytest.mark.parametrize("field",["fees_usdt","slippage_usdt","funding_usdt","final_equity_usdt"])
def test_reported_cash_metric_tampering_is_detected(field):
    snap,s=case()
    s=deepcopy(s)
    s["enhanced_chronological"][field]+=10
    with pytest.raises(ReplayDataError):
        audit_trace(snap,s)


def test_candle_turnover_quota_cannot_be_invented():
    snap,s=case()
    filled=s["enhanced_chronological"]["fills"][0]
    start=int(snap["signals"][0]["t"])
    for b in snap["candles"]:
        if b["t"]==start-BAR:
            b["amount"]=0
    with pytest.raises(ReplayDataError,match="PRIOR_BAR_SHARED_CAPACITY_EXCEEDED"):
        audit_trace(snap,s)


def test_forged_exit_buyback_price_is_detected():
    snap,s=case()
    s=deepcopy(s)
    exit_fill=s["enhanced_chronological"]["fills"][1]
    exit_fill["buyback_usdt"]+=10
    with pytest.raises(ReplayDataError,match="EXIT_MARKET_VALUE_NOT_CONSERVED"):
        audit_trace(snap,s)


def test_missing_close_fill_is_detected():
    snap,s=case()
    s=deepcopy(s)
    s["enhanced_chronological"]["fills"].pop()
    with pytest.raises(ReplayDataError,match="REPORTED_EXIT_FILL_COUNT_MISMATCH"):
        audit_trace(snap,s)


def test_two_uses_of_same_prior_liquidity_exceed_quota():
    snap,s=case()
    s=deepcopy(s)
    s["enhanced_chronological"]["fills"].append(
        deepcopy(s["enhanced_chronological"]["fills"][0])
    )
    with pytest.raises(ReplayDataError,match="DUPLICATE_OR_OVERLAPPING_ENTRY"):
        audit_trace(snap,s)


def test_delayed_stop_after_quota_consumed_reconciles_by_fills():
    snap=sample(stop_gap=True)
    ts=int(snap["signals"][0]["t"])
    for row in snap["candles"]:
        if row["t"]==ts-BAR:
            row["amount"]=10000.
        if row["t"]==ts+BAR:
            row.update(open=200.,high=205.,low=195.,close=200.)
    result=run_chronological(snap,cost_bps=100,mode="REFERENCE")
    assert result["counts"]["delayed_exit_fills"]==1
    out=audit_trace(snap,{
        "cost_bps_per_side":100,"lot_mode":"REFERENCE",
        "enhanced_chronological":result,
    })
    assert out["entries_checked"]==1
    assert out["exits_checked"]==1
    assert out["status"].startswith("V40_TRACE_LEDGER_ARITHMETIC_CONSISTENT")
