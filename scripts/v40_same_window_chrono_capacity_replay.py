"""Standalone chronological V40 same-window RESEARCH replay from PG snapshot.

Source-grounded mechanics:
* original Stage2 event tape and 49 strictly prior Min30 candles
* frozen V40 VOL30_SKIP, pooled shock budget and P10/P25/H4 sizing
* prior-bar 5% per-symbol shared fill quota
* independently documented 5x pooled proxy prior-close risk trims
* adverse-first SL75/trail14/gap0.5, partial stop fills and delayed
  forced buybacks at later candle opens
* funding using prior marked price, 8-bps fee and 100/200-bps scenario
  slippage on every separately filled entry, trim and exit
* known/current contract-lot rounding or explicit reference fallback

NOT the archived original research simulator: the sequencing of competing
opening trims and pending exits, simulated single-price fills, missing L2,
unverifiable point-in-time lots, risk-trim residuals and funding completeness
remain explicit approximations. A covered-window run is NOT a certification
or reliable live projection. No database connection, network, POST, or writes.

Run from repo root:
  .venv/bin/python -m scripts.v40_same_window_chrono_capacity_replay
"""
from __future__ import annotations

import argparse
import json
import math
from collections import Counter, defaultdict
from dataclasses import dataclass
from decimal import Decimal, ROUND_FLOOR
from pathlib import Path
from typing import Any

from app.t100_v40_admission import PriorBar, admission
from app.t100_200_strategy import (
    evaluate_completed_bar, trail_trigger_price, stop_price,
)
from scripts.v40_risk_trim_audit import planned_trims
from scripts.v40_same_window_pg_replay import (
    BAR, DEFAULT_END, DEFAULT_START, PAPER_START_USDT,
    ReplayDataError, epoch, finite, positive, lot_sizing,
    run_scenario, utc_iso,
)

MAX_EVENT_ROWS = 10000
MAX_BAR_ROWS = 250000
TOL = 1e-7
MAX_SLOTS = 4


@dataclass
class Holding:
    symbol: str
    signal_id: int
    started: int
    entry: float
    initial: float
    remaining: float
    mark: float
    lot: dict[str, Any] | None = None
    armed: bool = False
    best: float = 0.0
    pending: bool = False
    realized: float = 0.0
    funding: float = 0.0
    entry_cost: float = 0.0
    exit_fills: int = 0
    last_funding_ts: int = 0


def _validated_lot(
    specs: dict[str, Any], symbol: str, t: int, label: str,
) -> dict[str, Any] | None:
    if label != "CURRENT_METADATA_LOT_PROXY":
        return None
    rec = specs.get(symbol, {})
    meta = rec.get("metadata", {})
    if not isinstance(meta, dict):
        raise ReplayDataError("KNOWN_LOT_METADATA_INVALID")
    if rec.get("first_seen") is not None and int(rec["first_seen"]) > t:
        raise ReplayDataError("FUTURE_LOT_METADATA_USED")
    try:
        size = Decimal(str(meta["contractSize"]))
        step = Decimal(str(meta["volUnit"]))
        minimum = Decimal(str(meta["minVol"]))
    except (ValueError, KeyError, TypeError, ArithmeticError):
        raise ReplayDataError("KNOWN_LOT_METADATA_INVALID") from None
    if min(size, step, minimum) <= 0 or not all(
        x.is_finite() for x in (size, step, minimum)
    ):
        raise ReplayDataError("KNOWN_LOT_METADATA_INVALID")
    return {"size": size, "step": step, "minimum": minimum}


def _fill_entry_notional(
    p: Holding, requested_entry_notional: float,
    exit_price: float,
) -> float:
    """Return fillable *entry-basis* notional, rounded to known lots."""
    desired = min(p.remaining, requested_entry_notional)
    if desired <= TOL:
        return 0.0
    lot = p.lot
    if lot is None:
        return desired
    unit = lot["size"]*Decimal(str(p.entry))
    available_contracts = Decimal(str(p.remaining))/unit
    requested_contracts = Decimal(str(desired))/unit
    step = lot["step"]
    # Rounding down cannot accidentally increase liquidation quantity.
    wanted = (requested_contracts/step).to_integral_value(rounding=ROUND_FLOOR)*step
    if wanted < lot["minimum"]:
        return 0.0
    remaining_after = available_contracts-wanted
    if 0 < remaining_after < lot["minimum"]:
        # Never create a residual smaller than exchange min. Close fully
        # only when capacity supports it; otherwise retain min inventory.
        if requested_contracts >= available_contracts-Decimal("1e-10"):
            wanted = available_contracts
        else:
            wanted = (
                (available_contracts-lot["minimum"])/step
            ).to_integral_value(rounding=ROUND_FLOOR)*step
    if wanted < lot["minimum"] or wanted > available_contracts+Decimal("1e-9"):
        return 0.0
    filled = float(wanted*unit)
    return max(0.0, min(p.remaining, filled, desired+TOL))


def run_chronological(
    snapshot: dict[str, Any], *, cost_bps: int,
    mode: str = "HYBRID", initial_equity: float = PAPER_START_USDT,
) -> dict[str, Any]:
    """Replay a bounded period; cannot issue venue operations."""
    if type(cost_bps) is not int or cost_bps not in (100, 200):
        raise ReplayDataError("UNSUPPORTED_RESEARCH_COST")
    if mode not in ("REFERENCE", "HYBRID", "KNOWN_ONLY"):
        raise ReplayDataError("UNSUPPORTED_LOT_MODE")
    initial_equity = positive(initial_equity)
    start, end = int(snapshot["start"]), int(snapshot["end"])
    if end <= start or end-start > 45*86400:
        raise ReplayDataError("INVALID_REPLAY_WINDOW")
    if len(snapshot["candles"]) > MAX_BAR_ROWS:
        raise ReplayDataError("TOO_MANY_CANDLES")

    bars: dict[str, dict[int, dict[str, Any]]] = defaultdict(dict)
    for bar in snapshot["candles"]:
        sym, ts = str(bar["symbol"]), int(bar["t"])
        if ts % BAR or ts in bars[sym]:
            raise ReplayDataError("DUPLICATE_OR_NONALIGNED_CANDLE")
        op, hi, lo, cl = (positive(bar[k]) for k in ("open","high","low","close"))
        turnover = finite(bar["amount"])
        if lo > min(op,cl) or max(op,cl) > hi or turnover < 0:
            raise ReplayDataError("INVALID_MIN30_CANDLE")
        bars[sym][ts] = bar

    signal_at: dict[int,list[dict[str, Any]]] = defaultdict(list)
    seen_ids: set[int] = set()
    for sig in snapshot["signals"]:
        ts, sid = int(sig["t"]), int(sig["id"])
        if ts % BAR or sid in seen_ids or sid <= 0:
            raise ReplayDataError("DUPLICATE_OR_NONALIGNED_STAGE2_SIGNAL")
        seen_ids.add(sid)
        if start <= ts <= end:
            signal_at[ts].append(sig)
    for rows in signal_at.values():
        rows.sort(key=lambda x: int(x["id"]))

    funding: dict[str,list[dict[str, Any]]] = defaultdict(list)
    seen_funding: set[tuple[str,int]] = set()
    for item in snapshot["funding"]:
        sym, ts = str(item["symbol"]),int(item["t"])
        if (sym,ts) in seen_funding:
            raise ReplayDataError("DUPLICATE_FUNDING_EVENT")
        seen_funding.add((sym,ts))
        funding[sym].append(item)
    for lst in funding.values():
        lst.sort(key=lambda x:int(x["t"]))

    positions: dict[str,Holding] = {}
    closed: list[dict[str,Any]] = []
    fills: list[dict[str,Any]] = []
    skipped: list[dict[str,Any]] = []
    curve: list[dict[str,Any]] = []
    counts = Counter()
    cash = initial_equity
    fee = slip = funding_cash = 0.0
    peak = initial_equity
    max_dd = 0.0
    peak_exposure = 0.0
    highest_slots = 0
    near_term_incomplete = False
    account_bankrupt = False
    rates_bps = cost_bps/10000.0
    costs = .0008+rates_bps
    specs = snapshot.get("specs",{})
    first = ((start+BAR-1)//BAR)*BAR
    last = (end//BAR)*BAR

    def equity() -> float:
        return cash + sum(
            p.remaining*(1-p.mark/p.entry)
            for p in positions.values()
        )

    def used_capacity(t: int, symbol: str) -> float:
        return used[(symbol,t)]

    used: dict[tuple[str,int],float] = defaultdict(float)

    def cap(t: int,symbol: str) -> float:
        prior = bars[symbol].get(t-BAR)
        if prior is None:
            raise ReplayDataError(
                "MISSING_PRIOR_CANDLE_FOR_SHARED_CAPACITY"
            )
        return max(0.0,float(prior["amount"])*.05-used_capacity(t,symbol))

    def finish(p:Holding, t:int):
        if p.remaining > TOL:
            return
        p.remaining=0.0
        if p.symbol not in positions:
            raise ReplayDataError("DUPLICATE_POSITION_FINALIZATION")
        del positions[p.symbol]
        result = p.realized+p.funding-p.entry_cost
        counts["wins" if result>0 else "losses"]+=1
        closed.append({
            "signal_id":p.signal_id, "symbol":p.symbol,
            "entry_ts":utc_iso(p.started), "closed_ts":utc_iso(t),
            "initial_notional_usdt":round(p.initial,6),
            "net_pnl_usdt":round(result,6),
            "funding_usdt":round(p.funding,6),
            "exit_fills":p.exit_fills,
        })

    def exit_fill(
        p:Holding, *, t:int, price:float,
        requested_entry_notional:float, kind:str,
    ) -> float:
        nonlocal cash,fee,slip
        available = cap(t,p.symbol)
        max_entry_notional = available*p.entry/price
        requested = min(requested_entry_notional,max_entry_notional)
        closed_entry=_fill_entry_notional(p,requested,price)
        if closed_entry <= TOL:
            return 0.
        filled_usdt=closed_entry*price/p.entry
        if filled_usdt > available+1e-5:
            raise ReplayDataError("EXIT_CAPACITY_OVERRUN")
        gross=closed_entry-filled_usdt
        debit=filled_usdt*costs
        cash+=gross-debit
        p.realized+=gross-debit
        fee+=filled_usdt*.0008
        slip+=filled_usdt*rates_bps
        p.remaining=max(0.0,p.remaining-closed_entry)
        p.exit_fills+=1
        used[(p.symbol,t)]+=filled_usdt
        if len(fills)>=MAX_EVENT_ROWS:
            raise ReplayDataError("FILL_AUDIT_ROWS_EXCEEDED")
        fills.append({
            "symbol":p.symbol, "signal_id":p.signal_id,
            "t":utc_iso(t), "kind":kind,
            "entry_notional_closed_usdt":round(closed_entry,6),
            "buyback_usdt":round(filled_usdt,6),
            "price":round(price,12),
            "pending_after":p.remaining>TOL,
            "quota_used_usdt":round(used[(p.symbol,t)],6),
        })
        counts[kind+"_fills"]+=1
        finish(p,t)
        return closed_entry

    def record_equity(ts:int):
        nonlocal peak,max_dd,peak_exposure,account_bankrupt
        e=equity()
        if e<=0 or not math.isfinite(e):
            account_bankrupt=True
            return
        peak=max(peak,e)
        max_dd=min(max_dd,(e/peak-1)*100)
        notional=sum(p.remaining*p.mark/p.entry for p in positions.values())
        peak_exposure=max(peak_exposure,notional/e*100)
        curve.append({
            "t":utc_iso(ts),
            "equity":round(e,6),
            "gross_exposure_pct":round(notional/e*100,4),
            "open_positions":len(positions),
        })

    for now in range(first,last+1,BAR):
        # Do not rely on the partial current 30m OHLC for trigger or close.
        full_candle = now+BAR<=end

        # Strictly prior close marks and prior-entry funding only.
        for p in list(positions.values()):
            for f in funding[p.symbol]:
                t=int(f["t"])
                if p.last_funding_ts < t <= now and t>p.started:
                    paid=p.remaining*p.mark/p.entry*finite(f["rate"])
                    p.funding+=paid
                    cash+=paid
                    funding_cash+=paid
                    counts["funding_events"]+=1
            p.last_funding_ts=now

        # Opening mandatory pending exits are prioritized ahead of voluntary
        # trims, preventing an already triggered stop from being ignored.
        # The exact priority ordering is a research assumption, not source
        # certified from the archived simulator.
        for p in list(positions.values()):
            if not p.pending:
                continue
            row=bars[p.symbol].get(now)
            if row is None:
                near_term_incomplete=True
                break
            exit_fill(
                p,t=now,price=positive(row["open"]),
                requested_entry_notional=p.remaining,kind="delayed_exit",
            )
            if p.symbol in positions and p.remaining>TOL:
                counts["delayed_still_pending_bars"]+=1
        if near_term_incomplete:
            break

        # Frozen pooled risk trim based on pre-admission marked equity.
        marked={p.symbol:p.remaining*p.mark/p.entry for p in positions.values()}
        if marked:
            proposed=planned_trims(equity(),marked,cost_bps)
            for symbol,fraction in proposed.items():
                p=positions.get(symbol)
                if p is None or p.pending:
                    continue
                row=bars[symbol].get(now)
                if row is None:
                    near_term_incomplete=True
                    break
                desired=p.remaining*float(fraction)
                actual=exit_fill(
                    p,t=now,price=positive(row["open"]),
                    requested_entry_notional=desired,kind="trim",
                )
                if actual+TOL < desired:
                    counts["risk_trim_quota_or_lot_shortfall"]+=1
        if near_term_incomplete:
            break

        for sig in signal_at.get(now,[]):
            counts["eligible_stage2"]+=1
            symbol=str(sig["symbol"])
            if not (sig.get("strict365") is True and sig.get("stage1") is True
                    and finite(sig["r24"])>=.15 and finite(sig["atr7"])>=4.):
                counts["bad_stage1"]+=1
                continue
            if symbol in ("BTC_USDT","ETH_USDT"):
                counts["excluded"]+=1
                continue
            if symbol in positions:
                counts["duplicate_symbol"]+=1
                continue
            if len(positions)>=MAX_SLOTS:
                counts["capacity"]+=1
                continue
            raw=[bars[symbol].get(t) for t in range(now-49*BAR,now,BAR)]
            if len(raw)!=49 or any(b is None for b in raw):
                counts["missing_49_prior_bars"]+=1
                continue
            basis=positive(sig["price"])
            if abs(basis-positive(raw[-1]["close"]))>max(1e-8,basis*1e-6):
                counts["signal_candle_price_mismatch"]+=1
                continue
            prior49=[
                PriorBar(open_ts=int(b["t"]),high=float(b["high"]),
                         close=float(b["close"]),amount_usdt=float(b["amount"]))
                for b in raw
            ]
            e=equity()
            if e<=0:
                account_bankrupt=True
                break
            modeled=admission(
                prior_bars=prior49,admission_open_ts=now,equity_usdt=e,
                marked_existing_usdt=sum(marked.values()) if False else sum(
                    p.remaining*p.mark/p.entry for p in positions.values()
                ),
                stage1_atr7_pct=finite(sig["atr7"]),
                active_positions=len(positions),already_open_symbol=False,
                used_same_bar_usdt=used_capacity(now,symbol),
                cost_bps=float(cost_bps),
            )
            if modeled.action!="ADMIT":
                counts["v40_"+modeled.action+"_"+modeled.reason]+=1
                skipped.append({
                    "id":int(sig["id"]), "symbol":symbol,
                    "t":utc_iso(now),
                    "v40_reason":modeled.reason,
                    "vol_pct":modeled.pre24_vol_pct,
                })
                continue
            amount,label=lot_sizing(
                symbol,now,float(modeled.modeled_notional_usdt),
                basis,specs,mode,
            )
            if amount<=TOL:
                counts[label]+=1
                continue
            available=cap(now,symbol)
            if amount > available+1e-5:
                raise ReplayDataError("ENTRY_SHARED_CAPACITY_EXCEEDED")
            lot=_validated_lot(specs,symbol,now,label)
            positions[symbol]=Holding(
                symbol=symbol,signal_id=int(sig["id"]),started=now,
                entry=basis,initial=amount,remaining=amount,
                mark=basis,lot=lot,last_funding_ts=now,
                entry_cost=amount*costs,
            )
            cash-=amount*costs
            fee+=amount*.0008
            slip+=amount*rates_bps
            used[(symbol,now)]+=amount
            counts[label]+=1
            counts["entered"]+=1
            highest_slots=max(highest_slots,len(positions))
            if len(fills)>=MAX_EVENT_ROWS:
                raise ReplayDataError("FILL_AUDIT_ROWS_EXCEEDED")
            fills.append({
                "symbol":symbol,"signal_id":int(sig["id"]),
                "t":utc_iso(now),"kind":"entry",
                "filled_usdt":round(amount,6),
                "price":round(basis,12),
                "quota_used_usdt":round(used[(symbol,now)],6),
            })
        if account_bankrupt:
            break

        # Existing and just-admitted positions are checked against this
        # completed bar's pre-existing stop. New trail state is only used
        # from the NEXT completed bar. Normal exits occur AFTER admissions,
        # so they cannot free slots for an entry within the same bar.
        if full_candle:
            for p in list(positions.values()):
                row=bars[p.symbol].get(now)
                if row is None:
                    near_term_incomplete=True
                    break
                if p.pending:
                    p.mark=float(row["close"])
                    continue
                trigger=(
                    trail_trigger_price(p.entry,p.best,.5)
                    if p.armed else stop_price(p.entry)
                )
                decision=evaluate_completed_bar(
                    entry=p.entry,high=float(row["high"]),low=float(row["low"]),
                    trail_active=p.armed,best_profit_pct=p.best,
                )
                if decision.exit_price is not None:
                    actual=exit_fill(
                        p,t=now,
                        price=max(float(row["open"]),float(trigger)),
                        requested_entry_notional=p.remaining,
                        kind="normal_exit",
                    )
                    if p.symbol in positions:
                        p.pending=True
                        if actual<=TOL:
                            counts["trigger_no_capacity_or_lot"]+=1
                        else:
                            counts["partial_normal_exits"]+=1
                    counts["trigger_"+str(decision.reason)]+=1
                else:
                    p.armed=decision.trail_active
                    p.best=decision.best_profit_pct
                if p.symbol in positions:
                    p.mark=float(row["close"])
            if near_term_incomplete:
                break
            record_equity(now+BAR)
        else:
            # The final OHLC is incomplete; an entry at the bar open is
            # valid but the rest of this bar cannot be evaluated.
            for p in positions.values():
                q=snapshot.get("tickers",{}).get(p.symbol)
                if q is not None and 0<=end-int(q["t"])<=900:
                    p.mark=positive(q["price"])
                else:
                    # Prior completed candle close is at now, 5 minutes
                    # before the requested dashboard cutoff in this window.
                    if end-now>35*60:
                        near_term_incomplete=True
                    counts["terminal_prior_close_proxy_marks"]+=1
            if not near_term_incomplete:
                record_equity(end)
        if account_bankrupt or near_term_incomplete:
            break

    if account_bankrupt or near_term_incomplete:
        return {
            "status": (
                "INSOLVENT_EQUITY_REPLAY_HALTED"
                if account_bankrupt else
                "INCOMPLETE_REQUIRED_BAR_OR_FINAL_MARK_NO_RETURN"
            ),
            "cost_bps_per_side":cost_bps,
            "lot_mode":mode,"counts":dict(counts),
            "entry_allowed":False,"live_authorization":False,
        }

    final=equity()
    return {
        "status":"ENHANCED_CHRONOLOGICAL_V40_RESEARCH_PROXY_UNCERTIFIED",
        "lot_mode":mode,"cost_bps_per_side":cost_bps,
        "window_start_utc":utc_iso(start),"window_end_utc":utc_iso(end),
        "initial_equity_usdt":round(initial_equity,2),
        "final_equity_usdt":round(final,2),
        "return_pct":round((final/initial_equity-1)*100,2),
        "closed_trades":len(closed),"open_positions":len(positions),
        "pending_mandatory_exits":sum(p.pending for p in positions.values()),
        "max_half_hour_marked_drawdown_pct":round(max_dd,2),
        "peak_gross_exposure_pct":round(peak_exposure,2),
        "peak_open_slots":highest_slots,
        "funding_usdt":round(funding_cash,5),
        "fees_usdt":round(fee,5),"slippage_usdt":round(slip,5),
        "funding_completeness_certified":False,
        "current_contract_specs_historically_certified":False,
        "native_stop_execution_certified":False,
        "original_archived_simulator_equivalence_certified":False,
        "entry_allowed":False,"live_authorization":False,
        "counts":dict(counts),"skipped_signals":skipped,
        "trades":closed,"fills":fills,
        "equity_curve":curve,
    }


def comparison(snapshot:dict[str,Any], *, capital:float=2000.) -> dict[str,Any]:
    modes=("REFERENCE","HYBRID","KNOWN_ONLY")
    scenarios=[]
    for cost in (100,200):
        for mode in modes:
            upgraded=run_chronological(
                snapshot,cost_bps=cost,mode=mode,initial_equity=capital
            )
            prior=run_scenario(
                snapshot,cost_bps=cost,mode=mode,initial_equity=capital
            )
            scenarios.append({
                "cost_bps_per_side":cost,"lot_mode":mode,
                "prior_min30_proxy": {
                    "status":prior["status"],
                    "return_pct":prior.get("return_pct"),
                    "equity_usdt":prior.get("final_equity_usdt"),
                },
                "enhanced_chronological":upgraded,
                "delta_enhanced_minus_min30_pct_points":(
                    round(upgraded["return_pct"]-prior["return_pct"],2)
                    if "return_pct" in upgraded and "return_pct" in prior
                    else None
                ),
            })
    return {
        "status":"UNCERTIFIED_V40_RESEARCH_REPLAY_COMPARISON",
        "paper_reported_return_pct":96.88,
        "window_start_utc":utc_iso(int(snapshot["start"])),
        "window_end_utc":utc_iso(int(snapshot["end"])),
        "scenarios":scenarios,
        "limitations":[
            "Original archived V40 simulator source and all original intermediate fill records were NOT run on this new period.",
            "H4 is admission stressed-exit capacity horizon; actual exits are modeled with prior-Min30 5%-turnover quotas and delay, not assumed four-bar guaranteed liquidation.",
            "Opening pending stop versus risk-trim priority is an explicit research assumption.",
            "L2, queue execution, unavailable venue contracts, historical metadata changes and real liquidation unavailable.",
            "Funding completeness not independently certified; no historical lot metadata for UNKNOWN fallback.",
            "Opening/candle-event fills use one theoretical price rather than independently observed trading fills.",
            "No gap/liquidation/short squeeze beyond available candle evidence; drawdown is half-hourly marks, not trigger-aware.",
            "Any differences from the previous proxy are modeling sensitivity, not independently verified profit.",
        ],
        "real_money_execution_authorized":False,
    }


def main() -> None:
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument("--snapshot",default="~/Downloads/t100_v40_octnov_pg_snapshot.json")
    p.add_argument("--report-out",default="~/Downloads/t100_v40_octnov_chronological.json")
    p.add_argument("--starting-equity",type=float,default=2000.)
    args=p.parse_args()
    source=Path(args.snapshot).expanduser()
    data=json.loads(source.read_text())
    if (
        data.get("source")!="LIVE_POSTGRES_READ_ONLY_REPEATABLE_READ"
        or data.get("start")!=epoch(DEFAULT_START)
        or data.get("end")!=epoch(DEFAULT_END)
    ):
        raise SystemExit("FROZEN_POSTGRES_SNAPSHOT_PROVENANCE_OR_WINDOW_INVALID")
    report=comparison(data,capital=args.starting_equity)
    output=Path(args.report_out).expanduser()
    output.parent.mkdir(parents=True,exist_ok=True)
    output.write_text(json.dumps(report,indent=2,sort_keys=True)+"\n")
    output.chmod(0o600)
    print("READ_ONLY=true DATABASE_WRITES=0 MEXC_ORDERS=0")
    print("STATUS="+report["status"])
    print("REPORT_SAVED="+str(output))
    for s in report["scenarios"]:
        x=s["enhanced_chronological"]
        print(json.dumps({
            "cost_bps_per_side":s["cost_bps_per_side"],
            "lot_mode":s["lot_mode"],
            "prior_proxy_return_pct":s["prior_min30_proxy"].get("return_pct"),
            "chronological_return_pct":x.get("return_pct"),
            "chronological_equity_usdt":x.get("final_equity_usdt"),
            "chronological_drawdown_pct":x.get("max_half_hour_marked_drawdown_pct"),
            "open_positions":x.get("open_positions"),
            "pending_exits":x.get("pending_mandatory_exits"),
            "partial_fills":x.get("counts",{}).get("partial_normal_exits",0),
            "delayed_exit_fills":x.get("counts",{}).get("delayed_exit_fills",0),
            "risk_trim_fills":x.get("counts",{}).get("trim_fills",0),
            "status":x["status"],
        },sort_keys=True))


if __name__=="__main__":
    main()
