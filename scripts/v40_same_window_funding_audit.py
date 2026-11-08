"""Independent funding cashflow reconstruction for October/November V40 replay.

Consumes the previously exported read-only PostgreSQL snapshot and the output
of the *separate* chronological research simulator. This module does NOT
import or invoke that simulator's decisions, funding function or mutators.

Rebuilds each holding's quantity from recorded entry/exit fills, marks using
ONLY completed prior Min30 closes, applies archived funding rows at the first
30m boundary on/after settlement (after the position entry), and compares
each closed trade and aggregate funding cashflow to its report.

This validates consistency with the PostgreSQL funding rows AS EXPORTED.
It CANNOT prove missing funding rows did not exist, MEXC historical rates
were complete/correct, actual funding position values, or historical fills.
"""
from __future__ import annotations

import argparse
import json
import math
from collections import defaultdict
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from scripts.v40_same_window_pg_replay import (
    BAR, DEFAULT_START, DEFAULT_END, ReplayDataError, epoch, utc_iso,
)


@dataclass
class _Held:
    symbol: str
    signal_id: int
    entry_ts: int
    entry_price: float
    amount_entry_usdt: float
    last_mark_price: float
    last_funding_scan: int
    funding_cash_usdt: float = 0.


def _number(x: Any, *, strictly_positive: bool = False) -> float:
    if type(x) not in (int, float):
        raise ReplayDataError("FUNDING_AUDIT_NUMERIC_TYPE_INVALID")
    v=float(x)
    if not math.isfinite(v) or (v <= 0 if strictly_positive else v < 0):
        raise ReplayDataError("FUNDING_AUDIT_NONFINITE_OR_NEGATIVE")
    return v


def verify_source_funding(
    snapshot: dict[str, Any], scenario: dict[str, Any],
) -> dict[str, Any]:
    result=scenario["enhanced_chronological"]
    if result.get("status")!="ENHANCED_CHRONOLOGICAL_V40_RESEARCH_PROXY_UNCERTIFIED":
        return {"status":"INCOMPLETE_SCENARIO_NOT_FUNDING_VERIFIED"}
    if result.get("open_positions")!=0:
        return {"status":"OPEN_POSITION_FUNDING_NOT_FINALIZED"}
    start,end=int(snapshot["start"]),int(snapshot["end"])
    if end<=start or end-start>45*86400:
        raise ReplayDataError("FUNDING_AUDIT_BAD_WINDOW")
    if scenario["cost_bps_per_side"] not in (100,200):
        raise ReplayDataError("FUNDING_AUDIT_COST_INVALID")

    candles={}
    for bar in snapshot["candles"]:
        key=(str(bar["symbol"]),int(bar["t"]))
        if key in candles:
            raise ReplayDataError("FUNDING_AUDIT_DUPLICATE_CANDLE")
        candles[key]=bar

    events=defaultdict(list)
    funding_keys=set()
    for f in snapshot["funding"]:
        sym,ts=str(f["symbol"]),int(f["t"])
        if (sym,ts) in funding_keys:
            raise ReplayDataError("FUNDING_AUDIT_DUPLICATE_RATE")
        funding_keys.add((sym,ts))
        rate=f["rate"]
        if type(rate) not in (float,int) or not math.isfinite(float(rate)):
            raise ReplayDataError("FUNDING_AUDIT_INVALID_SIGNED_RATE")
        if not start<=ts<=end:
            raise ReplayDataError("FUNDING_AUDIT_RATE_OUT_OF_WINDOW")
        events[sym].append((ts,float(rate)))
    for items in events.values():
        items.sort()

    fills_by_time=defaultdict(list)
    seen_entries=set()
    entry_times={}
    for f in result["fills"]:
        ts=epoch(f["t"])
        if ts%BAR or not start<=ts<=end:
            raise ReplayDataError("FUNDING_AUDIT_FILL_TIME_INVALID")
        if f["kind"] not in ("entry","normal_exit","delayed_exit","trim"):
            raise ReplayDataError("FUNDING_AUDIT_UNKNOWN_FILL")
        fills_by_time[ts].append(f)
        if f["kind"]=="entry":
            sid=int(f["signal_id"])
            if sid in seen_entries:
                raise ReplayDataError("FUNDING_AUDIT_DUPLICATE_ENTRY")
            seen_entries.add(sid)
            entry_times[sid]=ts

    first=((start+BAR-1)//BAR)*BAR
    last=(end//BAR)*BAR
    holdings={}
    cash_by_id=defaultdict(float)
    n_events=0
    for now in range(first,last+1,BAR):
        # Funding is applied to holdings from the PREVIOUS completed
        # 30m close, before every entry, trim or stop fill of this bar.
        for sid,p in holdings.items():
            for ft,rate in events[p.symbol]:
                if p.last_funding_scan<ft<=now and ft>p.entry_ts:
                    paid=(
                        p.amount_entry_usdt*p.last_mark_price
                        /p.entry_price*rate
                    )
                    cash_by_id[sid]+=paid
                    n_events+=1
            p.last_funding_scan=now

        for f in fills_by_time.get(now,[]):
            sid=int(f["signal_id"])
            symbol=str(f["symbol"])
            kind=str(f["kind"])
            if kind=="entry":
                if sid in holdings or sid in cash_by_id:
                    raise ReplayDataError("FUNDING_AUDIT_ENTRY_ALREADY_EXISTS")
                entry=float(_number(f["filled_usdt"],strictly_positive=True))
                px=float(_number(f["price"],strictly_positive=True))
                holdings[sid]=_Held(
                    symbol=symbol,signal_id=sid,
                    entry_ts=now,entry_price=px,
                    amount_entry_usdt=entry,last_mark_price=px,
                    last_funding_scan=now,
                )
            else:
                p=holdings.get(sid)
                if p is None or p.symbol!=symbol:
                    raise ReplayDataError("FUNDING_AUDIT_EXIT_ORIGIN_INVALID")
                quantity=_number(f["entry_notional_closed_usdt"],strictly_positive=True)
                if quantity>p.amount_entry_usdt+.002:
                    raise ReplayDataError("FUNDING_AUDIT_EXIT_OVERCLOSE")
                p.amount_entry_usdt-=quantity
                if p.amount_entry_usdt < -0.002:
                    raise ReplayDataError("FUNDING_AUDIT_NEGATIVE_INVENTORY")
                if p.amount_entry_usdt<=.002:
                    del holdings[sid]

        if now+BAR<=end:
            for p in holdings.values():
                row=candles.get((p.symbol,now))
                if row is None:
                    raise ReplayDataError("FUNDING_AUDIT_REQUIRED_COMPLETED_BAR_MISSING")
                px=float(_number(row["close"],strictly_positive=True))
                p.last_mark_price=px

    if holdings:
        raise ReplayDataError("FUNDING_AUDIT_HOLDINGS_NONFLAT")
    reported_trades=result["trades"]
    if len(reported_trades)!=len(seen_entries):
        raise ReplayDataError("FUNDING_AUDIT_TRADE_COUNT_MISMATCH")
    for trade in reported_trades:
        sid=int(trade["signal_id"])
        if sid not in seen_entries:
            raise ReplayDataError("FUNDING_AUDIT_UNKNOWN_TRADE")
        reported=trade["funding_usdt"]
        if type(reported) not in (int,float) or not math.isfinite(reported):
            raise ReplayDataError("FUNDING_AUDIT_REPORTED_FUNDING_INVALID")
        if abs(cash_by_id[sid]-reported)>.002:
            raise ReplayDataError("FUNDING_AUDIT_TRADE_FUNDING_MISMATCH")

    total=sum(cash_by_id.values())
    report_total=result["funding_usdt"]
    if type(report_total) not in (int,float) or not math.isfinite(report_total):
        raise ReplayDataError("FUNDING_AUDIT_TOTAL_INVALID")
    if abs(total-report_total)>.003:
        raise ReplayDataError("FUNDING_AUDIT_AGGREGATE_MISMATCH")
    if n_events!=result["counts"].get("funding_events",0):
        raise ReplayDataError("FUNDING_AUDIT_EVENT_COUNT_MISMATCH")
    return {
        "status":"V40_EXPORTED_SOURCE_FUNDING_CASHFLOW_REBUILT_NONCERTIFYING",
        "scenario":{"cost_bps_per_side":scenario["cost_bps_per_side"],
                    "lot_mode":scenario["lot_mode"]},
        "funding_events_recomputed":n_events,
        "funding_cash_usdt_recomputed":round(total,5),
        "funding_cash_usdt_reported":report_total,
        "closed_trades_checked":len(reported_trades),
        "source_rates_replayed_independently":True,
        "funding_source_completeness_verified":False,
        "original_simulator_parity_verified":False,
        "historical_live_execution_verified":False,
        "live_authorization":False,
    }


def main() -> None:
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--snapshot",default="~/Downloads/t100_v40_octnov_pg_snapshot.json")
    parser.add_argument("--report",default="~/Downloads/t100_v40_octnov_chronological.json")
    parser.add_argument("--output",default="~/Downloads/t100_v40_octnov_funding_audit.json")
    args=parser.parse_args()
    snapshot=json.loads(Path(args.snapshot).expanduser().read_text())
    report=json.loads(Path(args.report).expanduser().read_text())
    if (
        snapshot.get("source")!="LIVE_POSTGRES_READ_ONLY_REPEATABLE_READ"
        or snapshot.get("start")!=epoch(DEFAULT_START)
        or snapshot.get("end")!=epoch(DEFAULT_END)
        or report.get("status")!="UNCERTIFIED_V40_RESEARCH_REPLAY_COMPARISON"
        or len(report.get("scenarios",[]))!=6
    ):
        raise SystemExit("FUNDING_AUDIT_INPUT_PROVENANCE_INVALID")
    try:
        results=[verify_source_funding(snapshot,s) for s in report["scenarios"]]
    except (ReplayDataError,KeyError,ValueError,TypeError) as exc:
        raise SystemExit(
            "FUNDING_AUDIT_FAILED: "+type(exc).__name__+" "+str(exc)
        ) from None
    path=Path(args.output).expanduser()
    path.parent.mkdir(parents=True,exist_ok=True)
    path.write_text(json.dumps(results,indent=2,sort_keys=True)+"\n")
    path.chmod(0o600)
    print("OFFLINE=true DATABASE_WRITES=0 EXCHANGE_ORDERS=0")
    print("AUDIT_SAVED="+str(path))
    for x in results:
        print(json.dumps(x,sort_keys=True))


if __name__=="__main__":
    main()
