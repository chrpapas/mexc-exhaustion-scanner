#!/usr/bin/env python3
from __future__ import annotations
import argparse, csv, json, math, re
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

PREFIX = "SNAPSHOT_AUDIT "


def parse_dt(v: str) -> datetime:
    return datetime.fromisoformat(v.replace("Z", "+00:00")).astimezone(timezone.utc)


def iter_payloads(paths: list[Path]) -> Iterable[dict[str, Any]]:
    seen = set()
    for path in paths:
        with path.open("r", encoding="utf-8", errors="replace") as f:
            for line in f:
                pos = line.find(PREFIX)
                if pos < 0:
                    continue
                raw = line[pos + len(PREFIX):].strip()
                try:
                    p = json.loads(raw)
                except json.JSONDecodeError:
                    continue
                key = (p.get("event"), p.get("cycle_started_at"), p.get("chunk_index"), p.get("chunk_count"))
                if key in seen:
                    continue
                seen.add(key)
                yield p


def linfit(xs: list[float], ys: list[float]) -> dict[str, float | None]:
    n = len(xs)
    if n < 2:
        return {"n": n, "slope": None, "intercept": None, "r": None, "rmse": None, "mae": None, "p95_abs_error": None}
    mx = sum(xs)/n; my = sum(ys)/n
    sxx = sum((x-mx)**2 for x in xs)
    syy = sum((y-my)**2 for y in ys)
    sxy = sum((x-mx)*(y-my) for x,y in zip(xs,ys))
    slope = sxy/sxx if sxx else 0.0
    intercept = my - slope*mx
    residuals = [y-(intercept+slope*x) for x,y in zip(xs,ys)]
    absr = sorted(abs(r) for r in residuals)
    idx = min(len(absr)-1, math.ceil(0.95*len(absr))-1)
    r = sxy/math.sqrt(sxx*syy) if sxx and syy else None
    return {
        "n": n, "slope": slope, "intercept": intercept, "r": r,
        "rmse": math.sqrt(sum(r*r for r in residuals)/n),
        "mae": sum(abs(r) for r in residuals)/n,
        "p95_abs_error": absr[idx],
    }


def median(values: list[float]) -> float | None:
    if not values: return None
    s = sorted(values); n = len(s); m = n//2
    return s[m] if n%2 else (s[m-1]+s[m])/2


def same_ohlc(a: dict[str,Any] | None, b: dict[str,Any] | None) -> bool:
    if not a or not b or a.get("open_time") != b.get("open_time"):
        return False
    try:
        return all(float(a[k]) == float(b[k]) for k in ("open","high","low","close"))
    except Exception:
        return False


def main() -> int:
    ap = argparse.ArgumentParser(description="Fit scanner candle-collector timing from SNAPSHOT_AUDIT Render logs")
    ap.add_argument("logs", nargs="+", help="raw Render log files containing SNAPSHOT_AUDIT lines")
    ap.add_argument("--output", default="~/trader-backtest/audit-timing-calibration")
    args = ap.parse_args()
    paths = [Path(p).expanduser().resolve() for p in args.logs]
    out = Path(args.output).expanduser().resolve(); out.mkdir(parents=True, exist_ok=True)

    candle_rows: list[dict[str,Any]] = []
    signal_reads: list[dict[str,Any]] = []
    retests: list[dict[str,Any]] = []
    cycle_meta: dict[str, dict[str,Any]] = {}

    for p in iter_payloads(paths):
        ev = p.get("event"); cs_raw = p.get("cycle_started_at"); rows = p.get("rows") or []
        if not cs_raw or not isinstance(rows,list): continue
        cs = parse_dt(cs_raw)
        if ev == "candle_cycle":
            meta = cycle_meta.setdefault(cs_raw, {"cycle_started_at":cs_raw,"symbol_count":p.get("symbol_count"),"request_concurrency":p.get("request_concurrency"),"failures":p.get("failures")})
            for r in rows:
                if not isinstance(r,dict) or not r.get("symbol"): continue
                x = dict(r); x["cycle_started_at"] = cs_raw
                try:
                    x["fetch_start_delta_s"] = (parse_dt(r["fetch_started_at"])-cs).total_seconds()
                    x["fetch_finish_delta_s"] = (parse_dt(r["fetch_finished_at"])-cs).total_seconds()
                    x["write_finish_delta_s"] = (parse_dt(r["write_finished_at"])-cs).total_seconds()
                    x["fetch_duration_s"] = (parse_dt(r["fetch_finished_at"])-parse_dt(r["fetch_started_at"])).total_seconds()
                    x["write_after_fetch_s"] = (parse_dt(r["write_finished_at"])-parse_dt(r["fetch_finished_at"])).total_seconds()
                except Exception:
                    pass
                candle_rows.append(x)
        elif ev == "signal_cycle":
            for r in rows:
                if not isinstance(r,dict): continue
                x = dict(r); x["signal_cycle_started_at"] = cs_raw
                if r.get("kind") == "signal_read": signal_reads.append(x)
                elif r.get("kind") == "retest_result": retests.append(x)

    # Deduplicate candle rows by cycle + queue index + symbol.
    unique = {}
    for r in candle_rows:
        unique[(r.get("cycle_started_at"),r.get("queue_index"),r.get("symbol"))] = r
    candle_rows = list(unique.values())

    by_cycle: dict[str,list[dict[str,Any]]] = defaultdict(list)
    by_symbol: dict[str,list[dict[str,Any]]] = defaultdict(list)
    for r in candle_rows:
        by_cycle[r["cycle_started_at"]].append(r); by_symbol[str(r["symbol"]).upper()].append(r)
    for arr in by_symbol.values():
        arr.sort(key=lambda r: parse_dt(r["write_finished_at"]) if r.get("write_finished_at") else datetime.min.replace(tzinfo=timezone.utc))

    cycle_reports=[]
    for cs_raw, rows in sorted(by_cycle.items()):
        usable=[r for r in rows if r.get("queue_index") is not None and r.get("fetch_start_delta_s") is not None]
        xs=[float(r["queue_index"]) for r in usable]; ys=[float(r["fetch_start_delta_s"]) for r in usable]
        fit=linfit(xs,ys)
        fetch_d=[float(r["fetch_duration_s"]) for r in usable if r.get("fetch_duration_s") is not None]
        waf=[float(r["write_after_fetch_s"]) for r in usable if r.get("write_after_fetch_s") is not None]
        write_end=[float(r["write_finish_delta_s"]) for r in usable if r.get("write_finish_delta_s") is not None]
        cs=parse_dt(cs_raw)
        phase=(cs.minute%15)*60 + cs.second + cs.microsecond/1e6
        cycle_reports.append({
            **cycle_meta.get(cs_raw,{}), "rows":len(rows), "phase_into_15m_s":phase,
            "fetch_start_slope_s_per_queue":fit["slope"], "fetch_start_intercept_s":fit["intercept"],
            "queue_fetch_r":fit["r"], "fit_rmse_s":fit["rmse"], "fit_mae_s":fit["mae"],
            "fit_p95_abs_error_s":fit["p95_abs_error"], "median_fetch_duration_s":median(fetch_d),
            "median_write_after_fetch_s":median(waf), "sweep_write_end_s":max(write_end) if write_end else None,
        })

    # Pair signal reads to latest preceding collector write for same symbol.
    pairs=[]
    for s in sorted(signal_reads, key=lambda r: parse_dt(r["read_at"])):
        sym=str(s.get("symbol") or "").upper(); read_at=parse_dt(s["read_at"])
        prev=[r for r in by_symbol.get(sym,[]) if r.get("write_finished_at") and parse_dt(r["write_finished_at"]) <= read_at]
        c=prev[-1] if prev else None
        consumed=s.get("completed_latest_min15") if isinstance(s.get("completed_latest_min15"),dict) else None
        fetched=c.get("latest_fetched") if c and isinstance(c.get("latest_fetched"),dict) else None
        same_open = bool(consumed and fetched and consumed.get("open_time") == fetched.get("open_time"))
        pairs.append({
            "symbol":sym,"read_at":s.get("read_at"),"signal_queue_index":s.get("queue_index"),
            "episode_id":s.get("episode_id"),"episode_state":s.get("episode_state"),
            "collector_cycle_started_at":c.get("cycle_started_at") if c else None,
            "collector_queue_index":c.get("queue_index") if c else None,
            "fetch_started_at":c.get("fetch_started_at") if c else None,
            "write_finished_at":c.get("write_finished_at") if c else None,
            "refresh_to_read_s":(read_at-parse_dt(c["write_finished_at"])).total_seconds() if c and c.get("write_finished_at") else None,
            "consumed_open_time":consumed.get("open_time") if consumed else None,
            "fetched_open_time":fetched.get("open_time") if fetched else None,
            "same_completed_candle_open_time":same_open,
            "consumed_matches_fetched_ohlc":same_ohlc(consumed,fetched),
        })

    slopes=[r["fetch_start_slope_s_per_queue"] for r in cycle_reports if r.get("fetch_start_slope_s_per_queue") is not None]
    intercepts=[r["fetch_start_intercept_s"] for r in cycle_reports if r.get("fetch_start_intercept_s") is not None]
    phases=[r["phase_into_15m_s"] for r in cycle_reports]
    sweeps=[r["sweep_write_end_s"] for r in cycle_reports if r.get("sweep_write_end_s") is not None]
    queue_sizes=[int(r["symbol_count"]) for r in cycle_reports if r.get("symbol_count")]
    symbol_queue_samples: dict[str,list[float]] = defaultdict(list)
    for r in candle_rows:
        if r.get("symbol") and r.get("queue_index") is not None:
            symbol_queue_samples[str(r["symbol"]).upper()].append(float(r["queue_index"]))
    symbol_queue_median={sym:median(vals) for sym,vals in sorted(symbol_queue_samples.items()) if vals}
    preceding_write=[r for r in pairs if r["write_finished_at"]]
    paired=[r for r in preceding_write if r["same_completed_candle_open_time"]]
    exact=[r for r in paired if r["consumed_matches_fetched_ohlc"]]
    outcomes={"confirmed":0,"invalidated":0,"expired":0,"waiting":0}
    for r in retests:
        if r.get("retest_confirmed"): outcomes["confirmed"]+=1
        elif r.get("retest_invalidated"): outcomes["invalidated"]+=1
        elif r.get("retest_expired"): outcomes["expired"]+=1
        else: outcomes["waiting"]+=1

    timing_model={
        "model_version":1,
        "generated_from":"SNAPSHOT_AUDIT candle cycles",
        "status":"provisional" if len(cycle_reports)<3 else "initial-fit",
        "source_cycle_count":len(cycle_reports),
        "phase_seconds_median":median(phases),
        "phase_samples_seconds":phases,
        "queue_slope_seconds_per_slot":median(slopes),
        "queue_intercept_seconds":median(intercepts) or 0.0,
        "queue_size_median":int(round(median(queue_sizes))) if queue_sizes else 401,
        "sweep_write_end_seconds_median":median(sweeps),
        "symbol_queue_index_median":symbol_queue_median,
        "intraminute_policy":"closed-minute-floor",
        "notes":[
            "Cycle phase is not assumed stable across worker restarts.",
            "Finalized Min1 data cannot recover arbitrary intraminute OHLC; current minute is excluded causally.",
            "Use exact audit overlays for forward certification; use this model for historical sensitivity/backtest reconstruction."
        ],
    }

    report={
        "input_logs":[str(p) for p in paths], "candle_cycles":len(cycle_reports),
        "candle_rows":len(candle_rows), "signal_reads":len(signal_reads), "retest_results":len(retests),
        "retest_outcomes":outcomes,
        "signal_reads_with_any_preceding_collector_write":len(preceding_write),
        "paired_signal_reads_same_completed_candle":len(paired),
        "paired_exact_ohlc":len(exact), "paired_exact_ohlc_rate":len(exact)/len(paired) if paired else None,
        "provisional_model":{
            "median_cycle_phase_into_15m_s":median(phases),
            "median_queue_delay_s_per_slot":median(slopes),
            "median_sweep_write_end_s":median(sweeps),
            "status":"provisional" if len(cycle_reports)<3 else "initial-fit",
            "reason":"need >=3 distinct full candle cycles to estimate phase/drift robustly" if len(cycle_reports)<3 else None,
        },
        "cycles":cycle_reports,
    }
    (out/"audit_timing_report.json").write_text(json.dumps(report,indent=2)+"\n",encoding="utf-8")
    (out/"timing_model.json").write_text(json.dumps(timing_model,indent=2,sort_keys=True)+"\n",encoding="utf-8")
    if cycle_reports:
        with (out/"audit_timing_cycles.csv").open("w",newline="",encoding="utf-8") as f:
            w=csv.DictWriter(f,fieldnames=list(cycle_reports[0].keys())); w.writeheader(); w.writerows(cycle_reports)
    if pairs:
        with (out/"audit_signal_pairs.csv").open("w",newline="",encoding="utf-8") as f:
            w=csv.DictWriter(f,fieldnames=list(pairs[0].keys())); w.writeheader(); w.writerows(pairs)
    print(json.dumps(report,indent=2))
    print("outputs:",out)
    return 0

if __name__ == "__main__":
    raise SystemExit(main())
