"""Detect event-clock disagreements between two independent same-window V40 proxies.

Reads already produced JSON only. Compares each Stage2 signal's entry time,
normal exit timestamp and funding-accounting interval under the earlier
Min30 proxy and the later capacity-limited chronological research model.

The purpose is to find timing discrepancies, NOT to designate either replay
as the original certified V40 chronological engine. No DB, API or writes
except an optional report JSON. Can reveal whether extra funding events
fall between the two modeled exit boundaries.
"""
from __future__ import annotations

import argparse
import json
import math
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any

from scripts.v40_same_window_pg_replay import (
    BAR, DEFAULT_START, DEFAULT_END, ReplayDataError, epoch,
)

MODES = ("REFERENCE", "HYBRID", "KNOWN_ONLY")
COSTS = (100, 200)


def audit_timing(
    snapshot: dict[str, Any],
    earlier_report: dict[str, Any],
    later_report: dict[str, Any],
) -> dict[str, Any]:
    if (
        type(snapshot) is not dict
        or snapshot.get("start") != epoch(DEFAULT_START)
        or snapshot.get("end") != epoch(DEFAULT_END)
        or earlier_report.get("status") != "RESEARCH_PROXY_NOT_CERTIFIED_FULL_V40_BACKTEST"
        or later_report.get("status") != "UNCERTIFIED_V40_RESEARCH_REPLAY_COMPARISON"
    ):
        raise ReplayDataError("WINDOW_OR_REPORT_PROVENANCE_MISMATCH")
    old_by_key={}
    for s in earlier_report["scenarios"]:
        key=(s["cost_bps_per_side"],s["lot_mode"])
        if key in old_by_key:
            raise ReplayDataError("DUPLICATE_OLD_SCENARIO")
        old_by_key[key]=s
    new_by_key={}
    for s in later_report["scenarios"]:
        key=(s["cost_bps_per_side"],s["lot_mode"])
        if key in new_by_key:
            raise ReplayDataError("DUPLICATE_NEW_SCENARIO")
        new_by_key[key]=s["enhanced_chronological"]
    keys={(cost, mode) for cost in COSTS for mode in MODES}
    if set(old_by_key)!=keys or set(new_by_key)!=keys:
        raise ReplayDataError("MISSING_SCENARIOS")

    funding_by_symbol=defaultdict(list)
    seen=set()
    for f in snapshot["funding"]:
        key=(f["symbol"],int(f["t"]))
        if key in seen:
            raise ReplayDataError("DUPLICATE_HISTORICAL_FUNDING_ROW")
        seen.add(key)
        funding_by_symbol[f["symbol"]].append(f)
    results=[]
    for key in sorted(keys):
        old,new=old_by_key[key],new_by_key[key]
        if (
            old["status"] != "INDICATIVE_V40_ENTRY_AND_MIN30_EXIT_PROXY_NOT_FULL_REPLAY"
            or new["status"] != "ENHANCED_CHRONOLOGICAL_V40_RESEARCH_PROXY_UNCERTIFIED"
            or old["open_positions"] != 0 or new["open_positions"] != 0
        ):
            results.append({
                "cost_bps_per_side":key[0],"lot_mode":key[1],
                "status":"NONFLAT_OR_INCOMPLETE_SCENARIO_NOT_COMPARABLE",
            })
            continue
        former={int(t["signal_id"]):t for t in old["closed"]}
        latter={int(t["signal_id"]):t for t in new["trades"]}
        if len(former)!=len(old["closed"]) or len(latter)!=len(new["trades"]):
            raise ReplayDataError("DUPLICATE_TRADE_ID")
        if set(former)!=set(latter):
            results.append({
                "cost_bps_per_side":key[0],"lot_mode":key[1],
                "status":"TRADE_SELECTION_OR_INVENTORY_DIVERGED",
                "earlier_ids_only":sorted(set(former)-set(latter)),
                "later_ids_only":sorted(set(latter)-set(former)),
            })
            continue

        changed=[]
        source_events_between_close_times=0
        offset_counts=Counter()
        old_funding_windows=0
        new_funding_windows=0
        for sid,a in sorted(former.items()):
            b=latter[sid]
            if a["symbol"]!=b["symbol"]:
                raise ReplayDataError("TRADE_ORIGIN_SYMBOL_MISMATCH")
            enter_a=epoch(a["entry_ts"])
            enter_b=epoch(b["entry_ts"])
            if enter_a!=enter_b:
                raise ReplayDataError("ENTRY_CLOCK_MISMATCH")
            exit_a=epoch(a["exit_ts"])
            exit_b=epoch(b["closed_ts"])
            if min(exit_a,exit_b)<enter_a:
                raise ReplayDataError("EXIT_PRECEDES_ENTRY")
            difference=exit_b-exit_a
            offset_counts[str(difference)]+=1
            historic=funding_by_symbol[a["symbol"]]
            # A funding event on a normal-exit timestamp is modeled
            # before the exit in both described replay conventions.
            count_a=sum(enter_a<int(f["t"])<=exit_a for f in historic)
            count_b=sum(enter_a<int(f["t"])<=exit_b for f in historic)
            between=abs(count_b-count_a)
            old_funding_windows+=count_a
            new_funding_windows+=count_b
            source_events_between_close_times+=between
            funding_diff=float(b["funding_usdt"])-float(a["funding_usdt"])
            if difference or abs(funding_diff)>0.000001:
                changed.append({
                    "signal_id":sid,"symbol":a["symbol"],
                    "prior_proxy_exit_ts":a["exit_ts"],
                    "chronological_exit_ts":b["closed_ts"],
                    "chronological_minus_prior_exit_seconds":difference,
                    "source_funding_events_prior_window":count_a,
                    "source_funding_events_chronological_window":count_b,
                    "source_events_between_exit_boundaries":between,
                    "prior_proxy_funding_usdt":a["funding_usdt"],
                    "chronological_funding_usdt":b["funding_usdt"],
                    "chronological_minus_prior_funding_usdt":round(funding_diff,6),
                })

        reported_old=int(old["counts"].get("funding_events",0))
        reported_new=int(new["counts"].get("funding_events",0))
        intervals_reconcile=(
            old_funding_windows==reported_old
            and new_funding_windows==reported_new
        )
        results.append({
            "cost_bps_per_side":key[0],"lot_mode":key[1],
            "status":(
                "MODELED_SETTLEMENT_WINDOWS_EXPLAIN_EVENT_COUNT_DIFFERENCE"
                if intervals_reconcile
                and (reported_old-reported_new)==(
                    old_funding_windows-new_funding_windows
                )
                else "FUNDING_EVENT_TIMING_DISCREPANCY_UNEXPLAINED"
            ),
            "trades_compared":len(former),
            "reported_old_funding_events":reported_old,
            "reported_new_funding_events":reported_new,
            "reported_event_count_difference_old_minus_new":reported_old-reported_new,
            "independently_counted_old_window_events":old_funding_windows,
            "independently_counted_new_window_events":new_funding_windows,
            "events_between_exit_boundaries":source_events_between_close_times,
            "exit_timestamp_delta_seconds_counts":dict(offset_counts),
            "changed_trade_events":changed,
            "event_timestamp_parity_with_original_certified":False,
            "historical_funding_source_completeness_certified":False,
        })
    return {
        "status":"CROSS_PROXY_EVENT_CLOCK_DIAGNOSTIC_NOT_ORIGINAL_PARITY",
        "scenarios":results,
        "original_v40_simulator_clock_verified":False,
    }


def main() -> None:
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument("--snapshot",default="~/Downloads/t100_v40_octnov_pg_snapshot.json")
    p.add_argument("--prior",default="~/Downloads/t100_v40_octnov_replay.json")
    p.add_argument("--enhanced",default="~/Downloads/t100_v40_octnov_chronological.json")
    p.add_argument("--output",default="~/Downloads/t100_v40_octnov_event_clock_audit.json")
    args=p.parse_args()
    source=json.loads(Path(args.snapshot).expanduser().read_text())
    older=json.loads(Path(args.prior).expanduser().read_text())
    later=json.loads(Path(args.enhanced).expanduser().read_text())
    report=audit_timing(source,older,later)
    target=Path(args.output).expanduser()
    target.parent.mkdir(parents=True,exist_ok=True)
    target.write_text(json.dumps(report,indent=2,sort_keys=True)+"\n")
    target.chmod(0o600)
    print("OFFLINE=true DATABASE_WRITES=0 EXCHANGE_ORDERS=0")
    print("AUDIT_SAVED="+str(target))
    for x in report["scenarios"]:
        print(json.dumps({k:v for k,v in x.items() if k!="changed_trade_events"},sort_keys=True))


if __name__=="__main__":
    main()
