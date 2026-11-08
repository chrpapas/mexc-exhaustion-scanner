"""No-network, no-database tests for V40-vs-paper dated attribution."""
from __future__ import annotations

from copy import deepcopy

import pytest

from scripts.v40_same_window_pg_replay import BAR, ReplayDataError, run_scenario
from scripts.v40_same_window_paper_attribution import explain_gap, _drawdown
from tests.test_v40_same_window_pg_replay import sample


def paper_fixture(snapshot, *, closed=True, net_pnl=15.0,
                  funding=.50, entry_fee=1.0,
                  entry_slippage=2.0, equity=None, close_after=False):
    signal=snapshot["signals"][0]
    first=int(signal["t"])
    end=int(snapshot["end"])
    position={
        "id":101,
        "signal_id":int(signal["id"]),
        "symbol":signal["symbol"],
        "tier":"LOW_100",
        "opened_at":first,
        "closed_at":end+BAR if close_after else first+BAR if closed else None,
        "db_status_now":"closed" if closed or close_after else "open",
        "entry_price":float(signal["price"]),
        "notional":100.,
        "entry_fee":entry_fee,
        "entry_slippage":entry_slippage,
        "net_pnl_excluding_entry_costs":net_pnl if closed or close_after else None,
        "exit_price":90. if closed or close_after else None,
        "exit_reason":"trail_gap0p5" if closed or close_after else None,
        "exit_fee":.20 if closed or close_after else 0.,
        "exit_slippage":1. if closed or close_after else 0.,
    }
    expected=net_pnl+funding-entry_fee-entry_slippage if closed and not close_after else None
    if equity is None:
        # For open-at-cutoff tests the synthetic constant-close candle gives
        # mark=100, so PnL is funding minus entry fees.
        equity=2000.+ (expected if expected is not None else funding-entry_fee-entry_slippage)
    return {
        "source":"SYNTHETIC_PAPER_DB",
        "start":snapshot["start"],
        "end":end,
        "positions":[position],
        "funding_applied":[{
            "position_id":101,
            "t":first+BAR,
            "pnl":funding,
        }],
        "equity_series":[
            {"t":first,"equity":2000., "gross_exposure_pct":50., "gross_notional":1000.,"open_positions":1},
            {"t":end-300,"equity":equity,"gross_exposure_pct":10.,"gross_notional":200.,"open_positions":0},
        ],
    }


def test_reproduces_closed_paper_contribution_with_entry_cost_and_funding():
    snap=sample(stop_gap=True)
    paper=paper_fixture(snap,closed=True)
    out=explain_gap(snap,paper)
    assert out["status"]=="DATED_PAPER_V40_ATTRIBUTION_RECONCILED_PROXY_ONLY"
    row=out["signals"][0]
    assert row["v40_decision"]=="V40_ENTERED"
    assert row["paper_contribution_usdt"]==12.5
    assert row["paper_status_at_cutoff"]=="CLOSED_BEFORE_CUTOFF"
    assert out["group_summary"]["V40_ENTERED"]["paper_contribution_usdt"]==12.5
    assert out["paper_contributions_reconcile_to_snapshot"]
    assert out["paper_snapshot_vs_trade_attribution_gap_usdt"]==0.0
    assert out["v40_proxy_contribution_crosscheck_usdt"]==pytest.approx(0,abs=.05)


def test_vol30_skipped_profitable_paper_trade_is_counted_as_missed():
    snap=sample(alternating=True)
    paper=paper_fixture(snap,closed=True,net_pnl=40.,funding=0.,entry_fee=0.,entry_slippage=0.)
    out=explain_gap(snap,paper)
    row=out["signals"][0]
    assert row["v40_decision"]=="V40_VOL30_SKIP"
    assert row["v40_volatility_pct"]>=30
    assert row["paper_contribution_usdt"]==40.
    assert row["v40_proxy_contribution_usdt"] is None
    assert out["group_summary"]["V40_VOL30_SKIP"]["paper_contribution_usdt"]==40
    assert out["group_summary"]["V40_ENTERED"]["signals"]==0
    assert out["status"]=="DATED_PAPER_V40_ATTRIBUTION_RECONCILED_PROXY_ONLY"


def test_position_closed_after_cutoff_uses_past_candle_not_future_exit():
    # V40 position must be closed inside the synthetic test window;
    # the paper position deliberately closes only *after* the cutoff.
    snap=sample(stop_gap=True)
    paper=paper_fixture(snap,closed=True,close_after=True,net_pnl=999.)
    out=explain_gap(snap,paper)
    assert out["signals"][0]["paper_status_at_cutoff"]=="OPEN_AT_CUTOFF_MARKED_COMPLETE_MIN30"
    assert out["signals"][0]["paper_contribution_usdt"]==-2.5
    assert out["signals"][0]["paper_exit_reason"] is None
    assert out["status"]=="DATED_PAPER_V40_ATTRIBUTION_RECONCILED_PROXY_ONLY"


def test_missing_asof_candle_blocks_paper_attribution():
    snap=sample(stop_gap=True)
    paper=paper_fixture(snap,closed=False)
    end=int(snap["end"])
    snap["candles"]=[b for b in snap["candles"] if b["t"]<end-3*BAR]
    out=explain_gap(snap,paper)
    assert out["status"]=="DATED_PAPER_V40_ATTRIBUTION_INCOMPLETE_DO_NOT_CLAIM"
    assert any("STALE_PAPER_ASOF_MARK" in x for x in out["missing_attribution_evidence"])


def test_paper_snapshot_contribution_mismatch_is_not_reconciled():
    snap=sample(stop_gap=True)
    paper=paper_fixture(snap,closed=True,equity=2200)
    out=explain_gap(snap,paper)
    assert out["status"]=="DATED_PAPER_V40_ATTRIBUTION_INCOMPLETE_DO_NOT_CLAIM"
    assert not out["paper_contributions_reconcile_to_snapshot"]
    assert out["paper_snapshot_vs_trade_attribution_gap_usdt"]==pytest.approx(187.5)


def test_missing_paper_position_blocks_reconciliation():
    snap=sample(stop_gap=True)
    paper=paper_fixture(snap)
    paper["positions"]=[]
    out=explain_gap(snap,paper)
    assert out["status"]=="DATED_PAPER_V40_ATTRIBUTION_INCOMPLETE_DO_NOT_CLAIM"
    assert not out["all_eligible_signals_in_paper_positions"]


def test_funding_is_added_only_once_to_paper_trade():
    snap=sample(stop_gap=True)
    paper=paper_fixture(snap,closed=True)
    paper["funding_applied"].append(deepcopy(paper["funding_applied"][0]))
    with pytest.raises(ReplayDataError,match="DUPLICATE_APPLIED_FUNDING"):
        explain_gap(snap,paper)


def test_window_mismatch_is_rejected():
    snap=sample(stop_gap=True)
    paper=paper_fixture(snap)
    paper["end"]+=BAR
    with pytest.raises(ReplayDataError,match="PAPER_V40_DATE_WINDOW_MISMATCH"):
        explain_gap(snap,paper)


def test_half_hour_drawdown_is_not_trigger_aware():
    curve=[
        {"t":1,"equity":2100.,"gross_exposure_pct":30},
        {"t":2,"equity":1800.,"gross_exposure_pct":120},
        {"t":3,"equity":2300.,"gross_exposure_pct":40},
    ]
    out=_drawdown(curve,2000.)
    assert out["sample_count"]==3
    assert out["drawdown_pct"]==pytest.approx((1800/2100-1)*100,abs=.0001)
    assert out["peak_gross_exposure_pct"]==120
    assert out["status"]=="PAPER_HALF_HOURLY_MARKS_NOT_TRIGGER_AWARE"
