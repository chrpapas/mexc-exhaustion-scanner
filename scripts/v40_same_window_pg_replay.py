"""Read-only PostgreSQL same-window T100/V40 replay (research, never orders).

Uses the live scanner's eligible Stage2 event tape, stored Min30 candles,
funding and current contract metadata. Replays repository V40 VOL30_SKIP/H4
causal 49-bar admission and modeled notional, with V39 lot quantization where
metadata exists. The P2 candidate source is shared with the paper system.

IMPORTANT: exits use the repository's frozen adverse-first SL75/trail14/
gap0.5 Min30 decision as a *proxy*. This is NOT the fully certified archived
V40 simulator's stressed H4 partial/delayed exit and pooled-risk trim path.
Current contract metadata is not point-in-time historical verification.
No claimed equivalence with the +96.88% paper result or future live fills.

All DB access is one read-only REPEATABLE READ transaction. No scanner or
trade process is started. No orders, inserts, updates, deletes, migrations,
service changes, credentials printed, or MEXC connections.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import math
import os
from collections import Counter, defaultdict
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any

from app.t100_v40_admission import PriorBar, admission, lot_quantize_v39
from app.t100_200_strategy import evaluate_completed_bar

DEFAULT_START = "2026-10-07T16:33:00+02:00"
DEFAULT_END = "2026-11-07T18:05:00+01:00"
PAPER_START_USDT = 2000.0
PAPER_END_USDT = 3937.68
BAR = 1800
FEE_RATE = .0008
MAX_SIGNAL_ROWS = 500
MAX_CANDLE_ROWS = 250000
FROZEN_DB_NAME = "mexc_exhaustion"


class ReplayDataError(ValueError):
    pass


def epoch(raw: str | datetime | int) -> int:
    if type(raw) is int:
        return raw
    stamp = datetime.fromisoformat(raw) if type(raw) is str else raw
    if type(stamp) is not datetime or stamp.tzinfo is None or stamp.utcoffset() is None:
        raise ReplayDataError("TIMESTAMP_MUST_INCLUDE_TIMEZONE")
    return int(stamp.timestamp())


def utc_iso(value: int) -> str:
    return datetime.fromtimestamp(value, timezone.utc).isoformat()


def positive(value: Any) -> float:
    try:
        v = float(value)
    except (ValueError, TypeError, OverflowError):
        raise ReplayDataError("NONNUMERIC_OR_MISSING_PRICE") from None
    if not math.isfinite(v) or v <= 0:
        raise ReplayDataError("NONPOSITIVE_OR_NONFINITE_PRICE")
    return v


def finite(value: Any) -> float:
    try:
        v = float(value)
    except (ValueError, TypeError, OverflowError):
        raise ReplayDataError("INVALID_FINITE_NUMBER") from None
    if not math.isfinite(v):
        raise ReplayDataError("INVALID_FINITE_NUMBER")
    return v


async def fetch_snapshot(dsn: str, *, start: int, end: int) -> dict[str, Any]:
    try:
        import asyncpg
    except ImportError as exc:
        raise ReplayDataError("ASYNC_PG_REQUIRED_INSTALL_PROJECT_DEPENDENCIES") from exc
    if end <= start or end-start > 45*86400:
        raise ReplayDataError("WINDOW_MUST_BE_POSITIVE_AND_AT_MOST_45_DAYS")
    conn = await asyncpg.connect(dsn=dsn, ssl="require", timeout=20, command_timeout=45)
    try:
        async with conn.transaction(isolation="repeatable_read", readonly=True):
            if await conn.fetchval("SELECT current_setting('transaction_read_only')") != "on":
                raise ReplayDataError("READ_ONLY_TRANSACTION_NOT_ENFORCED")
            if await conn.fetchval("SELECT current_database()") != FROZEN_DB_NAME:
                raise ReplayDataError("WRONG_DATABASE")
            start_dt = datetime.fromtimestamp(start, timezone.utc)
            end_dt = datetime.fromtimestamp(end, timezone.utc)
            raw_signals = await conn.fetch(
                """SELECT id,symbol,p2_at,entry_price,stage1_atr7_pct,
                          stage1_return_24h,stage1_strict365,stage1_eligible,
                          t100_tier,eligible_stage2,trade_decision
                   FROM public.t100_p2_signals
                   WHERE p2_at >= $1 AND p2_at <= $2
                         AND eligible_stage2=true
                   ORDER BY p2_at,id""",
                start_dt, end_dt,
            )
            if len(raw_signals) > MAX_SIGNAL_ROWS:
                raise ReplayDataError("TOO_MANY_STAGE2_SIGNALS")
            symbols = sorted({str(r["symbol"]) for r in raw_signals})
            if any(not s.endswith("_USDT") for s in symbols):
                raise ReplayDataError("UNEXPECTED_SIGNAL_SYMBOL")
            raw_candles = []
            raw_funding = []
            raw_contracts = []
            raw_tickers = []
            paper = None
            if symbols:
                raw_candles = await conn.fetch(
                    """SELECT symbol,open_time,open,high,low,close,amount
                       FROM public.candles
                       WHERE symbol=ANY($1::text[]) AND interval='Min30'
                         AND open_time >= $2 - interval '26 hours'
                         AND open_time < $3
                       ORDER BY symbol,open_time""",
                    symbols,start_dt,end_dt,
                )
                if len(raw_candles) > MAX_CANDLE_ROWS:
                    raise ReplayDataError("TOO_MANY_CANDLES")
                raw_funding = await conn.fetch(
                    """SELECT symbol,settle_time,funding_rate
                       FROM public.funding_rates
                       WHERE symbol=ANY($1::text[])
                         AND settle_time > $2 AND settle_time <= $3
                       ORDER BY symbol,settle_time""",
                    symbols,start_dt,end_dt,
                )
                raw_contracts = await conn.fetch(
                    """SELECT symbol,metadata,first_seen_at,last_seen_at
                       FROM public.contracts WHERE symbol=ANY($1::text[])""",
                    symbols,
                )
                raw_tickers = await conn.fetch(
                    """SELECT DISTINCT ON(symbol) symbol,observed_at,last_price
                       FROM public.ticker_snapshots
                       WHERE symbol=ANY($1::text[]) AND observed_at <= $2
                         AND observed_at >= $2 - interval '35 minutes'
                       ORDER BY symbol,observed_at DESC""",
                    symbols,end_dt,
                )
            paper_row = await conn.fetchrow(
                """SELECT snapshot_at,equity_usdt,open_positions
                   FROM public.t100_equity_snapshots
                   WHERE snapshot_at <= $1 AND snapshot_at >= $1 - interval '45 minutes'
                   ORDER BY snapshot_at DESC LIMIT 1""",
                end_dt,
            )
            if paper_row is not None:
                paper = {
                    "snapshot_at": epoch(paper_row["snapshot_at"]),
                    "equity_usdt": float(paper_row["equity_usdt"]),
                    "open_positions": int(paper_row["open_positions"]),
                }
    finally:
        await conn.close()
    candle_rows = [
        dict(
            symbol=str(r["symbol"]),t=epoch(r["open_time"]),
            open=positive(r["open"]),high=positive(r["high"]),
            low=positive(r["low"]),close=positive(r["close"]),
            amount=finite(r["amount"]),
        ) for r in raw_candles
    ]
    specs: dict[str, dict[str, Any]] = {}
    for r in raw_contracts:
        md = r["metadata"]
        if isinstance(md, str):
            md = json.loads(md)
        specs[str(r["symbol"])] = {
            "metadata": md if isinstance(md, dict) else {},
            "first_seen": epoch(r["first_seen_at"]) if r["first_seen_at"] else None,
            "last_seen": epoch(r["last_seen_at"]) if r["last_seen_at"] else None,
        }
    return {
        "source": "LIVE_POSTGRES_READ_ONLY_REPEATABLE_READ",
        "start": start, "end": end,
        "signals": [
            dict(
                id=int(r["id"]), symbol=str(r["symbol"]),
                t=epoch(r["p2_at"]), price=positive(r["entry_price"]),
                atr7=finite(r["stage1_atr7_pct"]),
                r24=finite(r["stage1_return_24h"]),
                strict365=r["stage1_strict365"] is True,
                stage1=r["stage1_eligible"] is True,
                tier=str(r["t100_tier"]),
                paper_decision=(str(r["trade_decision"]) if r["trade_decision"] else None),
            ) for r in raw_signals
        ],
        "candles": candle_rows,
        "funding": [
            dict(symbol=str(r["symbol"]),t=epoch(r["settle_time"]),
                 rate=finite(r["funding_rate"]))
            for r in raw_funding
        ],
        "specs": specs,
        "tickers": {
            str(r["symbol"]): {
                "t": epoch(r["observed_at"]), "price": positive(r["last_price"]),
            } for r in raw_tickers
        },
        "paper_db_snapshot": paper,
        "metadata_point_in_time_verified": False,
    }


@dataclass
class Position:
    symbol: str
    signal_id: int
    entry_ts: int
    entry: float
    notional: float
    last_price: float
    trail_active: bool = False
    best_profit_pct: float = 0.0
    funding: float = 0.0
    last_funding_scan_ts: int = 0


def lot_sizing(
    symbol: str, t: int, modeled: float, price: float,
    specs: dict[str, Any], mode: str,
) -> tuple[float, str]:
    if mode == "REFERENCE":
        return modeled, "REFERENCE_FRACTIONAL_NOTIONAL"
    raw = specs.get(symbol, {})
    md = raw.get("metadata", {})
    # Table contracts.metadata is overwritten by current scanner updates.
    # Its historical lot contract is never independently certified.
    first_seen = raw.get("first_seen")
    try:
        if first_seen is not None and first_seen > t:
            raise ValueError("metadata newer than entry")
        size = md["contractSize"]
        step = md["volUnit"]
        minimum = md["minVol"]
        maximum = md.get("maxVol")
        volume, notional, _ = lot_quantize_v39(
            modeled_notional=modeled, entry_price=price,
            contract_size=str(size), volume_step=str(step),
            min_volume=str(minimum),
            max_volume=str(maximum) if maximum is not None else None,
        )
        if volume <= 0:
            return 0., "KNOWN_SPEC_LOT_BELOW_MINIMUM"
        return float(notional), "CURRENT_METADATA_LOT_PROXY"
    except (KeyError, TypeError, ValueError, ArithmeticError, InvalidOperation):
        if mode == "KNOWN_ONLY":
            return 0., "HISTORICAL_CONTRACT_SPEC_UNKNOWN"
        return modeled, "UNKNOWN_SPEC_REFERENCE_FALLBACK"


def run_scenario(
    snapshot: dict[str, Any], *, cost_bps: int, mode: str,
    initial_equity: float = PAPER_START_USDT,
) -> dict[str, Any]:
    if cost_bps not in (100, 200) or mode not in ("REFERENCE", "HYBRID", "KNOWN_ONLY"):
        raise ReplayDataError("UNSUPPORTED_V40_SCENARIO")
    start, end = int(snapshot["start"]), int(snapshot["end"])
    if end <= start or end - start > 45*86400:
        raise ReplayDataError("BAD_SNAPSHOT_WINDOW")
    if positive(initial_equity) <= 0:
        raise ReplayDataError("BAD_INITIAL_EQUITY")
    bars: dict[str, dict[int, dict[str, Any]]] = defaultdict(dict)
    for row in snapshot["candles"]:
        symbol, t = row["symbol"], int(row["t"])
        if t in bars[symbol]:
            raise ReplayDataError("DUPLICATE_CANDLE")
        if not 0 < positive(row["low"]) <= positive(row["high"]):
            raise ReplayDataError("MALFORMED_CANDLE")
        if not row["low"] <= row["open"] <= row["high"] or not row["low"] <= row["close"] <= row["high"]:
            raise ReplayDataError("OHLC_INCONSISTENCY")
        bars[symbol][t] = row
    signals: dict[int, list[dict[str, Any]]] = defaultdict(list)
    seen_signal_ids: set[int] = set()
    for s in snapshot["signals"]:
        signal_id, signal_ts = int(s["id"]), int(s["t"])
        if signal_id in seen_signal_ids:
            raise ReplayDataError("DUPLICATE_STAGE2_SIGNAL_ID")
        seen_signal_ids.add(signal_id)
        # The archived V40 entry clock is a 30-minute boundary. Silent
        # omission of nonaligned scanner events would falsely show 0 trades.
        if signal_ts % BAR:
            raise ReplayDataError("NONALIGNED_STAGE2_EVENT_TIMESTAMP")
        if start <= signal_ts <= end:
            signals[signal_ts].append(s)
    for rows in signals.values():
        rows.sort(key=lambda x: int(x["id"]))
    funding: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in snapshot["funding"]:
        funding[row["symbol"]].append(row)
    for rows in funding.values():
        rows.sort(key=lambda x: int(x["t"]))
    counts = Counter()
    positions: list[Position] = []
    trades: list[dict[str, Any]] = []
    cost = FEE_RATE + cost_bps / 10000.
    cash = float(initial_equity)
    fee_total = slip_total = funding_total = 0.
    peak = float(initial_equity)
    max_dd = 0.
    exposure_max = 0.
    used_ticker = 0
    missing_exit = None
    first_boundary = ((start + BAR - 1) // BAR) * BAR
    last_boundary = end // BAR * BAR

    def equity() -> float:
        return cash + sum(
            p.notional * (p.entry - p.last_price)/p.entry
            for p in positions
        )

    for now in range(first_boundary, last_boundary + 1, BAR):
        prior = now - BAR
        for p in list(positions):
            bar = bars[p.symbol].get(prior)
            if bar is None:
                missing_exit = dict(symbol=p.symbol,bar_time=utc_iso(prior))
                break
            for f in funding[p.symbol]:
                ft = int(f["t"])
                if p.last_funding_scan_ts < ft <= now and ft > p.entry_ts:
                    paid = p.notional * p.last_price / p.entry * finite(f["rate"])
                    cash += paid
                    p.funding += paid
                    funding_total += paid
                    counts["funding_events"] += 1
            p.last_funding_scan_ts = now
            decision = evaluate_completed_bar(
                entry=p.entry, high=bar["high"],low=bar["low"],
                trail_active=p.trail_active,
                best_profit_pct=p.best_profit_pct,
            )
            if decision.exit_price is not None:
                # Short stop cannot fill more favorably than a gap-open above
                # the active trigger; adverse execution cost is additional.
                exit_px = max(float(decision.exit_price),float(bar["open"]))
                gross = p.notional * (p.entry-exit_px)/p.entry
                exit_value = p.notional * exit_px/p.entry
                exit_fee = exit_value * FEE_RATE
                exit_slip = exit_value * cost_bps / 10000.
                net = gross - exit_fee - exit_slip
                cash += net
                fee_total += exit_fee
                slip_total += exit_slip
                positions.remove(p)
                counts["exits_"+str(decision.reason)] += 1
                counts["wins" if net+p.funding > 0 else "losses"] += 1
                trades.append({
                    "signal_id":p.signal_id, "symbol":p.symbol,
                    "entry_ts":utc_iso(p.entry_ts), "exit_ts":utc_iso(now),
                    "entry":p.entry,"exit":exit_px,"notional_usdt":p.notional,
                    "exit_reason":decision.reason,
                    "net_exit_cash_usdt":round(net,6),
                    "funding_usdt":round(p.funding,6),
                })
            else:
                p.last_price = float(bar["close"])
                p.trail_active = decision.trail_active
                p.best_profit_pct = decision.best_profit_pct
        if missing_exit is not None:
            break

        for sig in signals.get(now, []):
            counts["eligible_stage2"] += 1
            symbol = sig["symbol"]
            if not (sig["strict365"] and sig["stage1"]
                    and finite(sig["r24"]) >= .15 and finite(sig["atr7"]) >= 4.):
                counts["invalid_stage1"] += 1
                continue
            if symbol in ("BTC_USDT", "ETH_USDT"):
                counts["excluded_symbol"] += 1
                continue
            if len(positions) >= 4:
                counts["capacity"] += 1
                continue
            if any(p.symbol == symbol for p in positions):
                counts["duplicate_symbol"] += 1
                continue
            prior49 = []
            for t in range(now-49*BAR,now,BAR):
                row = bars[symbol].get(t)
                if row is None:
                    break
                prior49.append(PriorBar(
                    open_ts=t,high=float(row["high"]),
                    close=float(row["close"]),amount_usdt=float(row["amount"]),
                ))
            if len(prior49) != 49:
                counts["missing_causal_49_bars"] += 1
                continue
            basis = positive(sig["price"])
            if abs(basis-float(prior49[-1].close)) > max(1e-8,basis*1e-6):
                counts["signal_basis_candle_mismatch"] += 1
                continue
            marked = sum(p.notional*p.last_price/p.entry for p in positions)
            eq = equity()
            if eq <= 0:
                counts["nonpositive_equity"] += 1
                continue
            decision = admission(
                prior_bars=prior49, admission_open_ts=now,
                equity_usdt=eq,marked_existing_usdt=marked,
                stage1_atr7_pct=finite(sig["atr7"]),
                active_positions=len(positions),already_open_symbol=False,
                cost_bps=float(cost_bps),
            )
            if decision.action != "ADMIT":
                counts["v40_"+decision.action+"_"+decision.reason] += 1
                continue
            # V40 amount limits are computed strictly from the completed
            # previous candle(s), not current or future market turnover.
            size,label = lot_sizing(
                symbol,now,float(decision.modeled_notional_usdt),
                basis,snapshot.get("specs",{}),mode,
            )
            if size <= 0:
                counts[label] += 1
                continue
            counts[label] += 1
            cash -= size*cost
            fee_total += size*FEE_RATE
            slip_total += size*cost_bps/10000.
            positions.append(Position(
                symbol=symbol,signal_id=int(sig["id"]),
                entry_ts=now,entry=basis,notional=size,
                last_price=basis,last_funding_scan_ts=now,
            ))
            counts["v40_entered"] += 1
        current_equity = equity()
        peak = max(peak,current_equity)
        max_dd = min(max_dd, (current_equity/peak-1.)*100.)
        exposure = sum(p.notional*p.last_price/p.entry for p in positions)
        if current_equity > 0:
            exposure_max = max(exposure_max,exposure/current_equity*100.)

    if missing_exit is not None:
        return {
            "status":"INCOMPLETE_POSITION_CANDLE_HISTORY_NO_PERFORMANCE_CLAIM",
            "missing":missing_exit,"counts":dict(counts),
            "cost_bps_per_side":cost_bps,"lot_mode":mode,
        }
    if positions:
        for p in positions:
            q=snapshot.get("tickers",{}).get(p.symbol)
            if q and 0 <= end-int(q["t"]) <= 900:
                p.last_price=positive(q["price"])
                used_ticker += 1
    final=equity()
    return {
        "status":"INDICATIVE_V40_ENTRY_AND_MIN30_EXIT_PROXY_NOT_FULL_REPLAY",
        "cost_bps_per_side":cost_bps,
        "lot_mode":mode,
        "initial_equity_usdt":round(initial_equity,2),
        "final_equity_usdt":round(final,2),
        "return_pct":round((final/initial_equity-1.)*100.,2),
        "paper_dashboard_return_pct":96.88,
        "difference_vs_paper_pct_points":round((final/initial_equity-1.)*100.-96.88,2),
        "closed_trades":len(trades),
        "open_positions":len(positions),
        "open_symbols":sorted(p.symbol for p in positions),
        "max_close_marked_drawdown_pct":round(max_dd,2),
        "peak_gross_exposure_pct":round(exposure_max,2),
        "funding_usdt":round(funding_total,4),
        "fees_usdt":round(fee_total,4),
        "slippage_usdt":round(slip_total,4),
        "funding_coverage_not_certified":True,
        "funding_records_present":bool(snapshot["funding"]),
        "current_contract_specs_not_historical":True,
        "v40_stressed_exit_partials_and_risk_trims_modeled":False,
        "terminal_prices_using_fresh_tickers":used_ticker,
        "counts":dict(counts),
        "closed":trades,
    }


def replay(snapshot: dict[str, Any], *, initial_equity: float) -> dict[str, Any]:
    scenarios = [
        run_scenario(snapshot,cost_bps=cost,mode=mode,initial_equity=initial_equity)
        for cost in (100,200)
        for mode in ("REFERENCE","HYBRID","KNOWN_ONLY")
    ]
    return {
        "status":"RESEARCH_PROXY_NOT_CERTIFIED_FULL_V40_BACKTEST",
        "data_source":snapshot.get("source"),
        "window_start_utc":utc_iso(int(snapshot["start"])),
        "window_end_utc":utc_iso(int(snapshot["end"])),
        "paper_dashboard": {
            "start_usdt":PAPER_START_USDT,
            "end_usdt":PAPER_END_USDT,
            "return_pct":96.88,
            "source":"User-supplied 2026-11-07 dashboard",
        },
        "paper_db_asof_snapshot":snapshot.get("paper_db_snapshot"),
        "eligible_stage2_signal_count":len(snapshot["signals"]),
        "min30_candle_count":len(snapshot["candles"]),
        "funding_record_count":len(snapshot["funding"]),
        "current_contract_metadata_count":len(snapshot.get("specs",{})),
        "limitations":[
            "Candidates are real Stage2 P2 records from the paper scanner, not independently re-generated signals.",
            "The repo V40 49-bar admission, VOL30_SKIP, pooled proxy and H4 capacity sizing are used.",
            "Exit path is adverse-first SL75/trail14/gap0.5 Min30 proxy, NOT original certified V40 H4 partial/delayed fills.",
            "V40 stressed exit liquidity, partial orders, funding completeness and pooled risk trims are not fully reproduced.",
            "Contracts table metadata is current and not point-in-time historical contract specifications.",
            "Reference and fallback sizes can be fractional/non-executable; HYBRID and KNOWN_ONLY are research comparisons.",
            "Reported high/low based replay cannot model order book, trading suspensions or actual intrabar path.",
        ],
        "scenarios":scenarios,
    }


def _parse_args() -> argparse.Namespace:
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument("--start",default=DEFAULT_START,help="ISO time WITH UTC offset")
    p.add_argument("--end",default=DEFAULT_END,help="ISO time WITH UTC offset")
    p.add_argument("--starting-equity",type=float,default=PAPER_START_USDT)
    p.add_argument("--dsn-env",default="DATABASE_URL",
                   help="env var holding Render Postgres URL; never printed")
    p.add_argument("--snapshot-in",default=None,
                   help="reuse a previously saved JSON snapshot; no DB connection")
    p.add_argument("--snapshot-out",default="~/Downloads/t100_v40_octnov_pg_snapshot.json")
    p.add_argument("--report-out",default="~/Downloads/t100_v40_octnov_replay.json")
    return p.parse_args()


def main() -> None:
    args=_parse_args()
    start,end=epoch(args.start),epoch(args.end)
    if end<=start or end-start>45*86400:
        raise SystemExit("INVALID_TIME_WINDOW")
    if args.snapshot_in:
        snap=json.loads(Path(args.snapshot_in).expanduser().read_text())
        if (snap.get("start"),snap.get("end")) != (start,end):
            raise SystemExit("SNAPSHOT_TIME_WINDOW_MISMATCH")
    else:
        dsn=os.environ.get(args.dsn_env,"").strip()
        if not dsn:
            raise SystemExit(
                "MISSING_DATABASE_URL: set a local environment variable with "
                "your Render Postgres external URL; do not paste it into chat."
            )
        try:
            snap=asyncio.run(fetch_snapshot(dsn,start=start,end=end))
        except Exception as exc:
            raise SystemExit("READ_ONLY_EXTRACTION_FAILED: "+type(exc).__name__) from None
        snapshot_file=Path(args.snapshot_out).expanduser()
        snapshot_file.parent.mkdir(parents=True,exist_ok=True)
        snapshot_file.write_text(json.dumps(snap,indent=2,sort_keys=True)+"\n")
        try:
            snapshot_file.chmod(0o600)
        except OSError:
            pass
        print("SNAPSHOT_SAVED="+str(snapshot_file))
    report=replay(snap,initial_equity=args.starting_equity)
    report_file=Path(args.report_out).expanduser()
    report_file.parent.mkdir(parents=True,exist_ok=True)
    report_file.write_text(json.dumps(report,indent=2,sort_keys=True)+"\n")
    try:
        report_file.chmod(0o600)
    except OSError:
        pass
    print("READ_ONLY=true; MEXC_ORDERS_SENT=0; DATABASE_WRITES=0")
    print("REPORT_SAVED="+str(report_file))
    print("DATE_MATCHED_RESEARCH_PROXY_NOT_CERTIFIED=true")
    for item in report["scenarios"]:
        print(json.dumps({
            k:item.get(k) for k in (
                "cost_bps_per_side","lot_mode","status",
                "return_pct","final_equity_usdt","closed_trades",
                "open_positions","counts",
            )
        },sort_keys=True))


if __name__=="__main__":
    main()
