"""Independent arithmetic and per-Min30 capacity audit of V40 research trace.

Consumes the *already generated* chronological JSON and its source snapshot.
Does not call its simulator, PostgreSQL, MEXC, Render or order services.
Checks complete flat scenarios only, recalculating entry/exit quantities,
5%-of-prior-bar turnover quotas, fees/slippage, funding summation and final
cash conservation from individual fills. Funding RATE correctness, trade
decisions, historic liquidity, queue execution, and true V40 historical
simulator equivalence are NOT certified by this audit.
"""
from __future__ import annotations

import argparse
import json
import math
from collections import defaultdict
from pathlib import Path
from typing import Any

from scripts.v40_same_window_pg_replay import (
    BAR, DEFAULT_END, DEFAULT_START, ReplayDataError, epoch,
)


def _safe_num(v: Any, *, allow_zero: bool = True) -> float:
    if type(v) not in (int,float):
        raise ReplayDataError("TRACE_NONNUMERIC")
    value=float(v)
    if not math.isfinite(value) or (value < 0 if allow_zero else value <= 0):
        raise ReplayDataError("TRACE_NONFINITE_OR_NEGATIVE_VALUE")
    return value


def _close(a: float, b: float, tolerance: float = 0.015) -> bool:
    return abs(a-b) <= tolerance


def audit_trace(
    source: dict[str,Any], scenario: dict[str,Any],
) -> dict[str,Any]:
    cost=scenario["cost_bps_per_side"]
    mode=scenario["lot_mode"]
    report=scenario["enhanced_chronological"]
    if type(cost) is not int or cost not in (100,200) or mode not in (
        "REFERENCE","HYBRID","KNOWN_ONLY"
    ):
        raise ReplayDataError("INVALID_SCENARIO_LABEL")
    if report.get("status") != "ENHANCED_CHRONOLOGICAL_V40_RESEARCH_PROXY_UNCERTIFIED":
        return {"status":"NO_AUDIT_INCOMPLETE_SIMULATOR_RUN",
                "cost_bps_per_side":cost,"lot_mode":mode}
    if report.get("open_positions") != 0:
        return {"status":"NO_FULL_CASH_AUDIT_OPEN_POSITION_MARKS_NOT_IN_LEDGER",
                "cost_bps_per_side":cost,"lot_mode":mode}
    if report.get("pending_mandatory_exits") != 0:
        raise ReplayDataError("PENDING_EXITS_WITH_FLAT_PORTFOLIO")

    start,end=int(source["start"]),int(source["end"])
    initial=_safe_num(report["initial_equity_usdt"],allow_zero=False)
    rows=report["fills"]
    trades=report["trades"]
    if type(rows) is not list or type(trades) is not list:
        raise ReplayDataError("TRACE_LEDGER_MISSING")
    candles={}
    for bar in source["candles"]:
        key=(str(bar["symbol"]),int(bar["t"]))
        if key in candles:
            raise ReplayDataError("DUPLICATE_SOURCE_CANDLE")
        candles[key]=bar

    entries={}
    remaining={}
    gross_exit_cash=0.0
    entry_cost_cash=0.0
    fee_total=0.0
    slip_total=0.0
    quota_used=defaultdict(float)
    pnl_before_funding=defaultdict(float)
    n_entries=n_exits=0

    for f in rows:
        kind=f["kind"]
        sym=str(f["symbol"])
        sid=int(f["signal_id"])
        ts=epoch(f["t"])
        px=_safe_num(f["price"],allow_zero=False)
        if not start <= ts <= end or ts % BAR:
            raise ReplayDataError("FILL_OUTSIDE_BOUNDED_CLOCK")
        prior=candles.get((sym,ts-BAR))
        if prior is None:
            raise ReplayDataError("MISSING_CAUSAL_LIQUIDITY_CANDLE")
        quota=_safe_num(prior["amount"])*.05
        key=(sym,ts)
        if kind=="entry":
            amount=_safe_num(f["filled_usdt"],allow_zero=False)
            if sid in entries or sym in (
                a["symbol"] for origin,a in entries.items()
                if remaining.get(origin,0)>1e-5
            ):
                raise ReplayDataError("DUPLICATE_OR_OVERLAPPING_ENTRY")
            entries[sid]={"symbol":sym,"entry":px,"notional":amount}
            remaining[sid]=amount
            n_entries+=1
            entry_cost_cash+=amount*(.0008+cost/10000)
            traded=amount
        elif kind in ("normal_exit","delayed_exit","trim"):
            if sid not in entries or entries[sid]["symbol"]!=sym:
                raise ReplayDataError("EXIT_WITHOUT_MATCHED_ENTRY")
            amount=_safe_num(f["entry_notional_closed_usdt"],allow_zero=False)
            buyback=_safe_num(f["buyback_usdt"],allow_zero=False)
            expected=amount*px/entries[sid]["entry"]
            if not _close(buyback,expected,.008):
                raise ReplayDataError("EXIT_MARKET_VALUE_NOT_CONSERVED")
            if amount>remaining[sid]+.008:
                raise ReplayDataError("EXIT_GREATER_THAN_REMAINING")
            remaining[sid]-=amount
            if remaining[sid]<-0.008:
                raise ReplayDataError("NEGATIVE_CONTRACT_INVENTORY")
            gross_exit_cash+=amount-buyback-buyback*(.0008+cost/10000)
            pnl_before_funding[sid]+=amount-buyback-buyback*(.0008+cost/10000)
            traded=buyback
            n_exits+=1
        else:
            raise ReplayDataError("UNKNOWN_SIMULATED_FILL_TYPE")

        fee_total+=traded*.0008
        slip_total+=traded*cost/10000
        quota_used[key]+=traded
        if quota_used[key]>quota+.018:
            raise ReplayDataError("PRIOR_BAR_SHARED_CAPACITY_EXCEEDED")
        if not _close(quota_used[key],_safe_num(f["quota_used_usdt"]),.015):
            raise ReplayDataError("REPORTED_CUMULATIVE_QUOTA_MISMATCH")

    if n_entries != int(report["counts"].get("entered",0)):
        raise ReplayDataError("REPORTED_ENTRY_COUNT_MISMATCH")
    if n_exits != sum(
        int(report["counts"].get(k+"_fills",0))
        for k in ("normal_exit","delayed_exit","trim")
    ):
        raise ReplayDataError("REPORTED_EXIT_FILL_COUNT_MISMATCH")
    if any(abs(left)>.015 for left in remaining.values()):
        raise ReplayDataError("OPEN_INVENTORY_IN_FLAT_SIMULATOR_REPORT")

    trade_ids=set()
    funding_total=0.0
    for t in trades:
        sid=int(t["signal_id"])
        if sid in trade_ids or sid not in entries:
            raise ReplayDataError("DUPLICATE_OR_UNKNOWN_CLOSED_TRADE")
        trade_ids.add(sid)
        entry=entries[sid]
        if str(t["symbol"])!=entry["symbol"]:
            raise ReplayDataError("CLOSED_TRADE_SYMBOL_MISMATCH")
        if not _close(_safe_num(t["initial_notional_usdt"]),entry["notional"],.015):
            raise ReplayDataError("CLOSED_TRADE_INITIAL_SIZE_MISMATCH")
        funding=_safe_num(abs(t["funding_usdt"]))
        signed_funding=float(t["funding_usdt"])
        if not math.isfinite(signed_funding):
            raise ReplayDataError("NONFINITE_FUNDING")
        expected_pnl=(
            pnl_before_funding[sid] + signed_funding
            -entry["notional"]*(.0008+cost/10000)
        )
        if not _close(expected_pnl,float(t["net_pnl_usdt"]),.02):
            raise ReplayDataError("TRADE_NET_PNL_DOES_NOT_RECONCILE")
        funding_total+=signed_funding
    if len(trades)!=len(entries) or len(trades)!=int(report["closed_trades"]):
        raise ReplayDataError("CLOSED_TRADES_DO_NOT_MATCH_FILLED_ENTRIES")

    if not _close(fee_total,_safe_num(report["fees_usdt"]),.02):
        raise ReplayDataError("FEE_LEDGER_MISMATCH")
    if not _close(slip_total,_safe_num(report["slippage_usdt"]),.02):
        raise ReplayDataError("SLIPPAGE_LEDGER_MISMATCH")
    if not _close(funding_total,float(report["funding_usdt"]),.02):
        raise ReplayDataError("FUNDING_LEDGER_MISMATCH")
    final=initial+gross_exit_cash-entry_cost_cash+funding_total
    if not _close(final,_safe_num(report["final_equity_usdt"]),.025):
        raise ReplayDataError("FINAL_EQUITY_NOT_CASH_CONSERVED")
    return {
        "status":"V40_TRACE_LEDGER_ARITHMETIC_CONSISTENT_NOT_EXECUTION_CERTIFIED",
        "cost_bps_per_side":cost, "lot_mode":mode,
        "entries_checked":n_entries,"exits_checked":n_exits,
        "shared_capacity_buckets_checked":len(quota_used),
        "closed_trades_checked":len(trades),
        "funding_total_usdt":round(funding_total,5),
        "recomputed_final_usdt":round(final,2),
        "final_reported_usdt":report["final_equity_usdt"],
        "historical_execution_certified":False,
        "funding_rates_independently_checked":False,
        "full_archived_V40_parity_certified":False,
    }


def main() -> None:
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument("--snapshot",default="~/Downloads/t100_v40_octnov_pg_snapshot.json")
    p.add_argument("--report",default="~/Downloads/t100_v40_octnov_chronological.json")
    p.add_argument("--output",default="~/Downloads/t100_v40_octnov_trace_audit.json")
    args=p.parse_args()
    source=json.loads(Path(args.snapshot).expanduser().read_text())
    report=json.loads(Path(args.report).expanduser().read_text())
    if (
        source.get("source")!="LIVE_POSTGRES_READ_ONLY_REPEATABLE_READ"
        or int(source.get("start",0))!=epoch(DEFAULT_START)
        or int(source.get("end",0))!=epoch(DEFAULT_END)
        or report.get("status")!="UNCERTIFIED_V40_RESEARCH_REPLAY_COMPARISON"
        or len(report.get("scenarios",[]))!=6
    ):
        raise SystemExit("SOURCE_PROVENANCE_OR_SCENARIO_COUNT_INVALID")
    try:
        result=[audit_trace(source,x) for x in report["scenarios"]]
    except (KeyError,ValueError,TypeError,ReplayDataError) as exc:
        raise SystemExit("V40_LEDGER_AUDIT_FAIL: "+type(exc).__name__+
                         " "+str(exc)) from None
    output=Path(args.output).expanduser()
    output.parent.mkdir(parents=True,exist_ok=True)
    output.write_text(json.dumps(result,indent=2,sort_keys=True)+"\n")
    output.chmod(0o600)
    print("OFFLINE=true DB_WRITES=0 EXCHANGE_ORDERS=0")
    print("LEDGER_AUDIT_SAVED="+str(output))
    for x in result:
        print(json.dumps(x,sort_keys=True))


if __name__=="__main__":
    main()
