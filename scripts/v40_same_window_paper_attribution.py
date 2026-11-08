"""Explain V40-vs-paper return gap from exact same dated PostgreSQL tape.

Requires the existing local snapshot from v40_same_window_pg_replay.py and a
read-only PostgreSQL connection to fetch *historical paper positions*,
funding-applied rows and equity snapshots. No order APIs or DB writes.

The paper contribution is a dated reconstruction of each entry, including
its entry costs, closed P&L or complete-Min30 candle mark, and only funding
applied on/before cutoff. Original database positions may now be closed;
they are treated as OPEN at the historical cutoff when closed_at > cutoff.

This is a comparison of V40's already labeled INDICATIVE 100/200-bps Min30
exit *proxy*, NOT a verified full V40 H4 partial/delayed-fill/risk-trim replay.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from app.t100_v40_admission import PriorBar, causal_49_bar_features
from scripts.v40_same_window_pg_replay import (
    BAR, DEFAULT_START, DEFAULT_END, PAPER_START_USDT, ReplayDataError,
    epoch, finite, positive, replay, utc_iso,
)

_MAX_PAPER_ROWS = 500
_MAX_EQUITY_ROWS = 5000
_DATABASE_NAME = "mexc_exhaustion"


async def fetch_paper_asof(
    dsn: str, *, snapshot: dict[str, Any],
) -> dict[str, Any]:
    """One bounded SSL connection; one REPEATABLE READ READ ONLY tx."""
    import asyncpg
    start, end = int(snapshot["start"]), int(snapshot["end"])
    ids = [int(s["id"]) for s in snapshot["signals"]]
    if not ids or len(ids) > _MAX_PAPER_ROWS or len(ids) != len(set(ids)):
        raise ReplayDataError("PAPER_SIGNAL_IDS_INVALID")
    conn = await asyncpg.connect(
        dsn=dsn, ssl="require", timeout=20, command_timeout=60,
    )
    stage = "VERIFY_TRANSACTION"
    try:
        async with conn.transaction(isolation="repeatable_read", readonly=True):
            if (
                await conn.fetchval("SELECT current_setting('transaction_read_only')") != "on"
                or await conn.fetchval("SELECT current_database()") != _DATABASE_NAME
            ):
                raise ReplayDataError("PAPER_DB_READ_ONLY_CONTEXT_INVALID")

            stage = "PAPER_POSITIONS"
            positions = await conn.fetch(
                """SELECT id,signal_id,symbol,tier,opened_at,closed_at,status,
                          entry_price,notional_usdt,entry_fee_usdt,
                          entry_slippage_usdt,net_pnl_usdt,exit_price,
                          exit_reason,exit_fee_usdt,exit_slippage_usdt
                   FROM public.t100_positions
                   WHERE signal_id=ANY($1::bigint[])
                   ORDER BY signal_id""", ids,
            )
            if len(positions) > len(ids):
                raise ReplayDataError("PAPER_DUPLICATE_POSITION_ROWS")
            position_ids = [int(p["id"]) for p in positions]

            stage = "PAPER_APPLIED_FUNDING"
            funding = await conn.fetch(
                """SELECT position_id,settle_time,pnl_usdt
                   FROM public.t100_funding_applied
                   WHERE position_id=ANY($1::bigint[])
                     AND settle_time >= $2 AND settle_time <= $3
                   ORDER BY position_id,settle_time""",
                position_ids,
                datetime.fromtimestamp(start, timezone.utc),
                datetime.fromtimestamp(end, timezone.utc),
            )

            stage = "PAPER_EQUITY_SERIES"
            equity = await conn.fetch(
                """SELECT snapshot_at,equity_usdt,gross_exposure_pct,
                          gross_notional_usdt,open_positions
                   FROM public.t100_equity_snapshots
                   WHERE snapshot_at >= $1 AND snapshot_at <= $2
                   ORDER BY snapshot_at""",
                datetime.fromtimestamp(start, timezone.utc),
                datetime.fromtimestamp(end, timezone.utc),
            )
            if len(equity) > _MAX_EQUITY_ROWS:
                raise ReplayDataError("PAPER_EQUITY_SERIES_TOO_LARGE")
    except ReplayDataError:
        raise
    except Exception as exc:
        sqlstate = getattr(exc, "sqlstate", None)
        safe_state = sqlstate if type(sqlstate) is str and len(sqlstate) == 5 and sqlstate.isascii() and sqlstate.isalnum() else "UNKNOWN"
        raise ReplayDataError(
            f"PAPER_DB_STAGE={stage} SQLSTATE={safe_state} ERROR_TYPE={type(exc).__name__}"
        ) from None
    finally:
        await conn.close()

    return {
        "source": "POSTGRES_READ_ONLY_REPEATABLE_READ",
        "start": start, "end": end,
        "positions": [{
            "id": int(p["id"]),
            "signal_id": int(p["signal_id"]),
            "symbol": str(p["symbol"]),
            "tier": str(p["tier"]),
            "opened_at": epoch(p["opened_at"]),
            "closed_at": epoch(p["closed_at"]) if p["closed_at"] is not None else None,
            "db_status_now": str(p["status"]),
            "entry_price": positive(p["entry_price"]),
            "notional": positive(p["notional_usdt"]),
            "entry_fee": finite(p["entry_fee_usdt"]),
            "entry_slippage": finite(p["entry_slippage_usdt"]),
            "net_pnl_excluding_entry_costs": (
                finite(p["net_pnl_usdt"]) if p["net_pnl_usdt"] is not None else None
            ),
            "exit_price": (
                positive(p["exit_price"]) if p["exit_price"] is not None else None
            ),
            "exit_reason": str(p["exit_reason"]) if p["exit_reason"] else None,
            "exit_fee": finite(p["exit_fee_usdt"]),
            "exit_slippage": finite(p["exit_slippage_usdt"]),
        } for p in positions],
        "funding_applied": [{
            "position_id": int(f["position_id"]),
            "t": epoch(f["settle_time"]),
            "pnl": finite(f["pnl_usdt"]),
        } for f in funding],
        "equity_series": [{
            "t": epoch(e["snapshot_at"]),
            "equity": finite(e["equity_usdt"]),
            "gross_exposure_pct": (
                finite(e["gross_exposure_pct"])
                if e["gross_exposure_pct"] is not None else None
            ),
            "gross_notional": finite(e["gross_notional_usdt"]),
            "open_positions": int(e["open_positions"]),
        } for e in equity],
    }


def _drawdown(curve: list[dict[str, Any]], initial_equity: float) -> dict[str, Any]:
    peak = positive(initial_equity)
    worst = 0.0
    peak_gross = 0.0
    for obs in sorted(curve, key=lambda x: x["t"]):
        e = finite(obs["equity"])
        if e <= 0:
            return {
                "status": "INVALID_NONPOSITIVE_EQUITY",
                "sample_count": len(curve),
                "drawdown_pct": None,
                "peak_gross_exposure_pct": None,
            }
        peak = max(peak, e)
        worst = min(worst, (e/peak-1.0)*100)
        if obs.get("gross_exposure_pct") is not None:
            peak_gross = max(peak_gross, finite(obs["gross_exposure_pct"]))
    return {
        "status": "PAPER_HALF_HOURLY_MARKS_NOT_TRIGGER_AWARE",
        "sample_count": len(curve),
        "drawdown_pct": round(worst, 4),
        "peak_gross_exposure_pct": round(peak_gross, 4),
        "first_snapshot_at": utc_iso(curve[0]["t"]) if curve else None,
        "last_snapshot_at": utc_iso(curve[-1]["t"]) if curve else None,
        "last_snapshot_equity_usdt": round(float(curve[-1]["equity"]), 4) if curve else None,
    }


def _volatility_by_signal(snapshot: dict[str, Any]) -> dict[int, float | None]:
    by_symbol: dict[str, dict[int, dict[str, Any]]] = defaultdict(dict)
    for b in snapshot["candles"]:
        by_symbol[str(b["symbol"])][int(b["t"])] = b
    out: dict[int, float | None] = {}
    for signal in snapshot["signals"]:
        t = int(signal["t"])
        history = [
            by_symbol[str(signal["symbol"])].get(k)
            for k in range(t-49*BAR, t, BAR)
        ]
        if any(x is None for x in history):
            out[int(signal["id"])] = None
            continue
        bars = [
            PriorBar(
                open_ts=int(b["t"]),high=float(b["high"]),
                close=float(b["close"]),amount_usdt=float(b["amount"]),
            ) for b in history
        ]
        try:
            features = causal_49_bar_features(bars, admit_open_ts=t)
        except (TypeError, ValueError, OverflowError):
            features = None
        out[int(signal["id"])] = float(features[0]) if features is not None else None
    return out


def explain_gap(
    snapshot: dict[str, Any], paper: dict[str, Any],
    *, cost_bps: int = 100, mode: str = "HYBRID",
    initial_equity: float = PAPER_START_USDT,
) -> dict[str, Any]:
    if snapshot["start"] != paper["start"] or snapshot["end"] != paper["end"]:
        raise ReplayDataError("PAPER_V40_DATE_WINDOW_MISMATCH")
    if positive(initial_equity) <= 0:
        raise ReplayDataError("INVALID_STARTING_EQUITY")
    scenarios = replay(snapshot, initial_equity=initial_equity)["scenarios"]
    scenario = next(
        (s for s in scenarios
         if s["cost_bps_per_side"] == cost_bps and s["lot_mode"] == mode),
        None,
    )
    if scenario is None:
        raise ReplayDataError("V40_SCENARIO_UNAVAILABLE")
    if not scenario["status"].startswith("INDICATIVE_V40"):
        raise ReplayDataError("V40_SCENARIO_INCOMPLETE_NO_COMPARISON")
    if scenario["open_positions"] != 0:
        raise ReplayDataError("V40_OPEN_POSITION_DETAIL_UNAVAILABLE")

    v40_by_id: dict[int, dict[str, Any]] = {}
    for t in scenario["closed"]:
        sid = int(t["signal_id"])
        if sid in v40_by_id:
            raise ReplayDataError("V40_DUPLICATE_ENTRY")
        v40_by_id[sid] = t
    signals = {int(s["id"]): s for s in snapshot["signals"]}
    if len(signals) != len(snapshot["signals"]):
        raise ReplayDataError("DUPLICATE_SIGNAL_ID")

    paper_by_signal = {int(p["signal_id"]): p for p in paper["positions"]}
    if len(paper_by_signal) != len(paper["positions"]):
        raise ReplayDataError("DUPLICATE_PAPER_SIGNAL_POSITION")
    if not set(paper_by_signal).issubset(signals):
        raise ReplayDataError("PAPER_POSITION_OUTSIDE_SIGNAL_SET")
    funding: dict[int, float] = defaultdict(float)
    seen_funding = set()
    for f in paper["funding_applied"]:
        fid = (int(f["position_id"]), int(f["t"]))
        if fid in seen_funding:
            raise ReplayDataError("DUPLICATE_APPLIED_FUNDING")
        seen_funding.add(fid)
        funding[fid[0]] += finite(f["pnl"])

    # Use only the last *completed* 30m candle ending at or before cutoff.
    cutoff = int(snapshot["end"])
    completed = defaultdict(dict)
    for c in snapshot["candles"]:
        ts = int(c["t"])
        if ts+BAR <= cutoff:
            completed[c["symbol"]][ts] = c
    vol = _volatility_by_signal(snapshot)
    rows: list[dict[str, Any]] = []
    gaps: list[str] = []
    for sid, sig in sorted(signals.items(), key=lambda z:(int(z[1]["t"]),z[0])):
        p = paper_by_signal.get(sid)
        v = v40_by_id.get(sid)
        signal_time = utc_iso(int(sig["t"]))
        selected = v is not None
        paper_result = None
        paper_status = "PAPER_SIGNAL_NOT_ENTERED"
        paper_size = None
        paper_exit_reason = None
        if p is not None:
            if p["symbol"] != sig["symbol"]:
                raise ReplayDataError("PAPER_SYMBOL_SIGNAL_MISMATCH")
            if int(p["opened_at"]) > cutoff:
                raise ReplayDataError("PAPER_POSITION_STARTS_AFTER_CUTOFF")
            paper_size = positive(p["notional"])
            entry_costs = finite(p["entry_fee"])+finite(p["entry_slippage"])
            funding_part = funding.get(int(p["id"]),0.0)
            paper_exit_reason = p["exit_reason"]
            if p["closed_at"] is not None and int(p["closed_at"]) <= cutoff:
                if p["net_pnl_excluding_entry_costs"] is None:
                    gaps.append("MISSING_CLOSED_PAPER_PNL:"+str(sid))
                    paper_status="PAPER_CLOSED_PNL_MISSING"
                else:
                    paper_result = (
                        finite(p["net_pnl_excluding_entry_costs"])
                        + funding_part-entry_costs
                    )
                    paper_status = "CLOSED_BEFORE_CUTOFF"
            else:
                candidates = [
                    (ts,row) for ts,row in completed[p["symbol"]].items()
                    if ts+BAR <= cutoff and ts >= int(p["opened_at"])-BAR
                ]
                if not candidates:
                    gaps.append("MISSING_PAPER_ASOF_MARK:"+str(sid))
                    paper_status = "PAPER_OPEN_ASOF_PRICE_MISSING"
                else:
                    mark_t, mark = max(candidates, key=lambda z:z[0])
                    # Older than 35m means stale mark, not a valid cutoff P&L.
                    if cutoff-(mark_t+BAR)>35*60:
                        gaps.append("STALE_PAPER_ASOF_MARK:"+str(sid))
                        paper_status="PAPER_OPEN_ASOF_MARK_STALE"
                    else:
                        entry=positive(p["entry_price"])
                        paper_result=paper_size*(entry-positive(mark["close"]))/entry
                        paper_result+=funding_part-entry_costs
                        paper_status="OPEN_AT_CUTOFF_MARKED_COMPLETE_MIN30"

        v40_pnl = None
        v40_size = None
        if v is not None:
            v40_size = positive(v["notional_usdt"])
            # The simulator debits per-side entry fee+slippage to cash.
            v40_pnl = (
                finite(v["net_exit_cash_usdt"])
                +finite(v["funding_usdt"])
                -v40_size*(.0008+cost_bps/10000.)
            )
        volatility=vol.get(sid)
        if not selected and volatility is not None and volatility>=30:
            reason="V40_VOL30_SKIP"
        elif selected:
            reason="V40_ENTERED"
        else:
            reason="V40_OTHER_OR_UNVERIFIED"
        rows.append({
            "signal_id":sid,"symbol":sig["symbol"],"signal_utc":signal_time,
            "paper_tier":sig.get("tier"),"v40_volatility_pct":(
                round(volatility,4) if volatility is not None else None
            ),
            "v40_decision":reason,
            "paper_status_at_cutoff":paper_status,
            "paper_exit_reason":paper_exit_reason if paper_status=="CLOSED_BEFORE_CUTOFF" else None,
            "paper_notional_usdt":round(paper_size,4) if paper_size else None,
            "v40_notional_usdt":round(v40_size,4) if v40_size else None,
            "v40_over_paper_size_pct":round(100*v40_size/paper_size,2)
            if v40_size is not None and paper_size is not None else None,
            "paper_contribution_usdt":round(paper_result,4) if paper_result is not None else None,
            "v40_proxy_contribution_usdt":round(v40_pnl,4) if v40_pnl is not None else None,
        })

    groups={}
    for label in ("V40_ENTERED","V40_VOL30_SKIP","V40_OTHER_OR_UNVERIFIED"):
        cohort=[r for r in rows if r["v40_decision"]==label]
        groups[label]={
            "signals":len(cohort),
            "paper_notional_total_usdt":round(sum(r["paper_notional_usdt"] or 0 for r in cohort),2),
            "paper_contribution_usdt":round(sum(r["paper_contribution_usdt"] or 0 for r in cohort),2),
            "paper_contribution_complete":all(r["paper_contribution_usdt"] is not None for r in cohort),
            "v40_proxy_contribution_usdt":round(sum(r["v40_proxy_contribution_usdt"] or 0 for r in cohort),2),
        }

    paper_curve = _drawdown(paper["equity_series"],initial_equity)
    v40_total = sum(r["v40_proxy_contribution_usdt"] or 0 for r in rows)
    v40_expected = finite(scenario["final_equity_usdt"])-initial_equity
    all_paper_positions_seen=len(paper_by_signal)==len(signals)
    paper_contrib_total=sum(r["paper_contribution_usdt"] or 0 for r in rows)
    paper_curve_asof=paper_curve.get("last_snapshot_equity_usdt")
    paper_equity_delta=(
        paper_curve_asof-initial_equity
        if paper_curve_asof is not None else None
    )
    paper_attribution_gap=(
        paper_equity_delta-paper_contrib_total
        if paper_equity_delta is not None else None
    )
    paper_curve_current=(
        bool(paper["equity_series"])
        and cutoff-int(paper["equity_series"][-1]["t"]) <= 35*60
    )
    paper_contributions_match=(
        paper_attribution_gap is not None
        and abs(paper_attribution_gap) <= max(2.0, initial_equity*.005)
    )
    # Aggregate funding can be missing due DB retention, and the paper
    # snapshot may differ from the Discord mark at 18:05. Do not force
    # contributions into equality with the dashboard's +96.88%.
    return {
        "status": (
            "DATED_PAPER_V40_ATTRIBUTION_RECONCILED_PROXY_ONLY"
            if all_paper_positions_seen and not gaps and paper_curve_current
               and paper_contributions_match
               and abs(v40_total-v40_expected)<.05
            else "DATED_PAPER_V40_ATTRIBUTION_INCOMPLETE_DO_NOT_CLAIM"
        ),
        "scenario": {"cost_bps_per_side":cost_bps,"lot_mode":mode},
        "window_start_utc":utc_iso(int(snapshot["start"])),
        "window_end_utc":utc_iso(cutoff),
        "paper_dashboard_user_reported_return_pct":96.88,
        "v40_proxy_return_pct":scenario["return_pct"],
        "v40_proxy_max_half_hour_drawdown_pct":scenario["max_close_marked_drawdown_pct"],
        "v40_proxy_peak_gross_exposure_pct":scenario["peak_gross_exposure_pct"],
        "paper_half_hour_history":paper_curve,
        "paper_asof_equity_delta_usdt":round(paper_equity_delta,4)
        if paper_equity_delta is not None else None,
        "summed_paper_trade_contributions_usdt":round(paper_contrib_total,2),
        "paper_snapshot_vs_trade_attribution_gap_usdt":round(
            paper_attribution_gap,2
        ) if paper_attribution_gap is not None else None,
        "paper_snapshot_recent_enough_for_cutoff":paper_curve_current,
        "paper_contributions_reconcile_to_snapshot":paper_contributions_match,
        "v40_proxy_contribution_crosscheck_usdt":round(v40_total-v40_expected,4),
        "all_eligible_signals_in_paper_positions":all_paper_positions_seen,
        "missing_attribution_evidence":gaps,
        "group_summary":groups,
        "signals":rows,
        "limitations":[
            "V40 exit and funding are indicative Min30 proxy, not full original V40 partial/delayed H4 and pooled risk-trim path.",
            "Paper closed P&L is as recorded in its DB, open positions are marked to last completed 30m candle.",
            "Paper rows can have been updated since cutoff; only time-based immutable close/funding rows can be back-dated.",
            "Paper funding coverage and V40 funding coverage are not independently certified.",
            "Half-hour peak-to-trough drawdowns are not comparable to trigger-aware intrabar drawdowns.",
            "Current MEXC contract metadata is not historical contract-spec verification.",
            "The V40 scenario's stronger execution-cost assumption is already deducted from its contribution.",
        ],
    }


def _parse_args():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument("--snapshot",default="~/Downloads/t100_v40_octnov_pg_snapshot.json")
    p.add_argument("--report-out",default="~/Downloads/t100_v40_octnov_attribution.json")
    p.add_argument("--dsn-env",default="DATABASE_URL")
    p.add_argument("--cost-bps",type=int,choices=(100,200),default=100)
    p.add_argument("--lot-mode",choices=("REFERENCE","HYBRID","KNOWN_ONLY"),default="HYBRID")
    p.add_argument("--starting-equity",type=float,default=PAPER_START_USDT)
    return p.parse_args()


def main() -> None:
    args=_parse_args()
    snap=json.loads(Path(args.snapshot).expanduser().read_text())
    if (
        type(snap) is not dict
        or epoch(DEFAULT_START)!=snap.get("start")
        or epoch(DEFAULT_END)!=snap.get("end")
        or snap.get("source")!="LIVE_POSTGRES_READ_ONLY_REPEATABLE_READ"
    ):
        raise SystemExit("SNAPSHOT_PROVENANCE_OR_WINDOW_INVALID")
    dsn=os.environ.get(args.dsn_env,"").strip()
    if not dsn:
        raise SystemExit("MISSING_DATABASE_URL: do not paste credentials in chat")
    try:
        paper=asyncio.run(fetch_paper_asof(dsn,snapshot=snap))
        result=explain_gap(
            snap,paper,cost_bps=args.cost_bps,
            mode=args.lot_mode,initial_equity=args.starting_equity,
        )
    except ReplayDataError as exc:
        raise SystemExit("ATTRIBUTION_HALTED: "+str(exc)) from None
    except Exception as exc:
        raise SystemExit("ATTRIBUTION_HALTED: "+type(exc).__name__) from None
    path=Path(args.report_out).expanduser()
    path.parent.mkdir(parents=True,exist_ok=True)
    path.write_text(json.dumps(result,indent=2,sort_keys=True)+"\n")
    path.chmod(0o600)
    print("READ_ONLY=true; DATABASE_WRITES=0; MEXC_ORDERS=0")
    print("REPORT_SAVED="+str(path))
    print("STATUS="+result["status"])
    print("PAPER_HALF_HOUR_DD="+str(result["paper_half_hour_history"]["drawdown_pct"]))
    print("V40_PROXY_DD="+str(result["v40_proxy_max_half_hour_drawdown_pct"]))
    print("PAPER_PEAK_EXPOSURE="+str(result["paper_half_hour_history"]["peak_gross_exposure_pct"]))
    print("V40_PROXY_PEAK_EXPOSURE="+str(result["v40_proxy_peak_gross_exposure_pct"]))
    print("COHORT_SUMMARY="+json.dumps(result["group_summary"],sort_keys=True))
    print("PAPER_ATTRIBUTION_VS_SNAPSHOT_GAP_USDT="+str(
        result["paper_snapshot_vs_trade_attribution_gap_usdt"]
    ))
    print("SKIPPED_SIGNAL_ROWS="+json.dumps(
        [r for r in result["signals"] if r["v40_decision"]=="V40_VOL30_SKIP"],
        sort_keys=True,
    ))


if __name__=="__main__":
    main()
