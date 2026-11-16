"""Research trace audit. Uses fixed exported trades, never executes the strategy or orders.

--immutable-candles is ONLY for a verified and dormant historical SQLite
snapshot, and refuses pending transaction side files. Optional
--candidate-archive re-evaluates 146 original Stage2 gate/occupancy/sizing
decisions against per-bar recreated account state based on PRESCRIBED fills.
It does not certify independent Stage2 discovery, independent exit fills,
historical contract rules, venue execution, or live trading.
"""

import csv, json, sqlite3, hashlib, io
from zipfile import ZipFile
from collections import defaultdict
from datetime import datetime
from decimal import Decimal as D
from pathlib import Path

start = int(datetime.fromisoformat("2023-02-03T02:30:00+00:00").timestamp())
end = int(datetime.fromisoformat("2026-09-25T08:30:00+00:00").timestamp())


def ts(s):
    return int(datetime.fromisoformat(s).timestamp())


def conn(path, immutable=False):
    path = Path(path).expanduser().resolve()
    if not path.is_file():
        raise ValueError("HISTORICAL_DATABASE_NOT_FOUND")
    if immutable and any(
        Path(str(path) + suffix).exists() for suffix in ("-wal", "-journal")
    ):
        raise ValueError("UNSNAPSHOTTED_SQLITE_JOURNAL")
    c = sqlite3.connect(
        path.as_uri() + "?mode=ro" + ("&immutable=1" if immutable else ""),
        uri=True,
    )
    try:
        c.execute("PRAGMA query_only=ON")
        c.execute("BEGIN")
    except BaseException:
        c.close()
        raise
    return c


def _pinned_candidate_data(path):
    """Load exactly the original 146 V40 Stage2 candidates and gated decisions."""
    from app.t100_live_strategy_parity_gate import FROZEN_V40_ARCHIVE_SHA256
    h=hashlib.sha256()
    with Path(path).expanduser().open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024*1024),b""):
            h.update(chunk)
    if h.hexdigest()!=FROZEN_V40_ARCHIVE_SHA256:
        raise ValueError("FROZEN_CANDIDATE_ARCHIVE_SHA_MISMATCH")
    with ZipFile(path) as z:
        def rows(name):
            member=z.getinfo(name)
            if not 0<member.file_size<10_000_000:
                raise ValueError("FROZEN_ARCHIVE_MEMBER_SIZE_INVALID")
            return list(csv.DictReader(io.StringIO(z.read(name).decode("utf-8-sig"))))
        manifest=json.loads(z.read("manifest.json"))
        if (manifest.get("candidate_count")!=146 or
            manifest.get("preceding_candles_exported")!=7154 or
            manifest.get("type")!="READ_ONLY_SOURCE_PARITY_SAMPLE"):
            raise ValueError("FROZEN_CANDIDATE_MANIFEST_INVALID")
        candidate_rows=rows("candidates.csv")
        history_rows=rows("prior49_bars.csv")
        gates={}
        for cost in (100,200):
            label=f"V32_VOL30_SKIP_H4_S{cost}_CAP5_V39_HYBRID"
            gates[cost]={
                (r["symbol"],ts(r["time"])):r["action"]
                for r in rows("capital_2000/gate_decisions.csv")
                if r["scenario"]==label
            }
    if len(candidate_rows)!=146 or len(history_rows)!=7154 or any(
        len(gates[cost])!=132 for cost in (100,200)
    ):
        raise ValueError("FROZEN_CANDIDATE_COUNTS_INVALID")
    by_time=defaultdict(list)
    keys=set()
    for r in candidate_rows:
        key=(r["symbol"],int(r["entry_ts"]))
        if key in keys or key[1]%1800:
            raise ValueError("FROZEN_CANDIDATE_KEYS_INVALID")
        keys.add(key)
        by_time[key[1]].append(r)
    if any(k not in keys or v not in ("ADMIT","SKIP")
           for m in gates.values() for k,v in m.items()):
        raise ValueError("FROZEN_GATES_INVALID")
    bars=defaultdict(list)
    for r in history_rows:
        key=(r["signal_symbol"],int(r["admission_ts"]))
        if key not in keys:
            raise ValueError("FROZEN_CANDIDATE_PRIOR_BAR_ORPHAN")
        bars[key].append((int(r["open_ts"]),r["high"],r["close"],r["amount"]))
    if len(bars)!=146 or any(len(v)!=49 for v in bars.values()):
        raise ValueError("FROZEN_CANDIDATE_PRIOR_BAR_COUNT_INVALID")
    return by_time, gates, bars


def audit(root, folders, start=start, end=end, audit_trims=False, entry_specs=None,
          *, candles_path=None, funding_path=None, immutable_candles=False,
          candidate_archive=None):
    """Audit prescribed historical ledgers, never execute live orders."""
    if candidate_archive is not None and (
        entry_specs is None or len(folders)!=1 or "historical" not in folders
        or start!=globals()["start"] or end!=globals()["end"]
    ):
        raise ValueError("CANDIDATE_REPLAY_REQUIRES_COMPLETE_FROZEN_HISTORICAL_PATH")
    candidate_data = (_pinned_candidate_data(candidate_archive)
                      if candidate_archive is not None else None)
    candles = conn(
        candles_path or root / "historical-market-data-min30-v1/historical-market-data-min30-v1.sqlite",
        immutable=immutable_candles,
    )
    funds = conn(
        funding_path or root / "mexc-certification-cache-v1.sqlite", True
    )
    results = {}
    for cohort, folder in folders.items():
        raw = {
            n: list(csv.DictReader((folder / (n + ".csv")).open()))
            for n in ("entries", "exits", "fills", "metrics")
        }
        for cost in (100, 200):
            scenario = f"V32_VOL30_SKIP_H4_S{cost}_CAP5_V39_HYBRID"
            rows = {n: [r for r in v if r["scenario"] == scenario] for n, v in raw.items()}
            m = rows["metrics"][0]
            rate = D(".0008") + D(cost) / 10000
            entries = defaultdict(list)
            exits = defaultdict(list)
            funding = defaultdict(list)
            entry_prices = {}
            for r in rows["entries"]:
                entries[ts(r["time"])].append(r)
            for r in rows["exits"]:
                exits[ts(r["exit_time"])].append(r)
            for r in rows["fills"]:
                if r["kind"] == "entry":
                    entry_prices[(r["symbol"], ts(r["time"]))] = D(r["price"])
            for symbol in {r["symbol"] for r in rows["entries"]}:
                for settle, value in funds.execute(
                    "SELECT settle_ts,rate FROM funding WHERE symbol=? AND settle_ts>=? AND settle_ts<=?",
                    (symbol, start, end),
                ):
                    funding[(settle + 1799) // 1800 * 1800].append((symbol, settle, D(str(value))))
            cash = D(2000)
            peak = cash
            close_dd = D(0)
            high_dd = D(0)
            active = {}
            entry_diff = D(0)
            fund = D(0)
            event_count = 0
            trim_plans = 0
            trim_fills = 0
            sizing_checked = 0
            sizing_max_difference = D(0)
            candidate_decisions_checked = 0
            candidate_gate_skips = 0
            candidate_portfolio_rejects = 0
            accepted_candidate_decisions = 0

            def equity(prices=None, index=0):
                return cash + sum(
                    (
                        p["n"]
                        * (1 - (prices[s][index] if prices and s in prices else p["mark"]) / p["entry"])
                        for s, p in active.items()
                    ),
                    D(0),
                )

            def close(row):
                nonlocal cash
                p = active[row["symbol"]]
                n = D(row["notional"])
                price = D(row["exit"])
                cash += n * (1 - price / p["entry"]) - n * price / p["entry"] * rate
                p["n"] -= n
                if p["n"] < D("-.001"):
                    raise ValueError("OVER_CLOSE")
                if abs(p["n"]) < D(".001"):
                    del active[row["symbol"]]

            for t in range(start, end + 1, 1800):
                staged_candidates=(candidate_data[0].get(t,[]) if candidate_data is not None else [])
                syms = (set(active) | {r["symbol"] for r in entries[t]}
                        | {r["symbol"] for r in staged_candidates})
                if not syms:
                    continue
                prices = {}
                for sym, op, hi, cl in candles.execute(
                    "SELECT symbol,open,high,close FROM candles WHERE interval='Min30' AND open_ts=? AND symbol IN ("
                    + ",".join("?" for _ in syms)
                    + ")",
                    (t, *sorted(syms)),
                ):
                    value = tuple(D(str(v)) for v in (op, hi, cl))
                    if sym in prices and prices[sym] != value:
                        raise ValueError("CONFLICTING_CANDLE")
                    prices[sym] = value
                for sym, settle, frate in funding[t]:
                    if sym in active and active[sym]["time"] < settle:
                        p = active[sym]
                        v = p["n"] * p["mark"] / p["entry"] * frate
                        cash += v
                        fund += v
                        event_count += 1
                if audit_trims:
                    from scripts.v40_risk_trim_audit import planned_trims
                    plans = planned_trims(equity(), {
                        sym: p["n"]*p["mark"]/p["entry"] for sym, p in active.items()
                    }, cost)
                    trim_plans += len(plans)
                for sym, p in active.items():
                    if sym in prices:
                        p["mark"] = prices[sym][0]
                for row in exits[t]:
                    if row["kind"] in ("trim", "delayed_exit"):
                        if audit_trims and row["kind"] == "trim":
                            sym = row["symbol"]
                            if sym not in plans or D(row["notional"]) > active[sym]["n"]*plans[sym]+D('.001'):
                                raise ValueError('TRIM_EXCEEDS_PRIOR_CLOSE_PLAN')
                            trim_fills += 1
                        close(row)
                if candidate_data is not None and staged_candidates:
                    from app.t100_v40_admission import PriorBar, admission, lot_quantize_v39
                    ledger_entries={(r["symbol"],t):r for r in entries[t]}
                    shadow={sym:dict(p) for sym,p in active.items()}
                    shadow_cash=cash
                    candidate_accepted=[]
                    for candidate in staged_candidates:
                        sym=candidate["symbol"]
                        key=(sym,t)
                        history=[PriorBar(ts0,float(hi),float(cl),float(amount))
                                 for ts0,hi,cl,amount in candidate_data[2][key]]
                        source_history=candles.execute(
                            "SELECT open_ts,high,close,amount FROM candles "
                            "WHERE symbol=? AND interval='Min30' AND open_ts>=? "
                            "AND open_ts<? ORDER BY open_ts",
                            (sym,t-49*1800,t)).fetchall()
                        if len(source_history)!=49 or any(
                            int(observed[0])!=b.open_ts or
                            abs(float(observed[1])-b.high)>1e-6 or
                            abs(float(observed[2])-b.close)>1e-6 or
                            abs(float(observed[3])-b.amount_usdt)>max(1e-6,abs(b.amount_usdt)*1e-12)
                            for observed,b in zip(source_history,history)
                        ):
                            raise ValueError("CANDIDATE_CAUSAL_HISTORY_MISMATCH")
                        shadow_equity=shadow_cash+sum((
                            p["n"]*(1-p["mark"]/p["entry"]) for p in shadow.values()
                        ),D(0))
                        marked=sum((p["n"]*p["mark"]/p["entry"] for p in shadow.values()),D(0))
                        used=sum((
                            D(x["notional"])*D(x["exit"])/D(x["entry"])
                            for x in exits[t] if x["symbol"]==sym
                            and x["kind"] in ("trim","delayed_exit")
                        ),D(0))
                        decision=admission(
                            prior_bars=history, admission_open_ts=t,
                            equity_usdt=float(shadow_equity),
                            marked_existing_usdt=float(marked),
                            stage1_atr7_pct=float(candidate["stage1_atr7_pct"]),
                            active_positions=len(shadow),
                            already_open_symbol=sym in shadow,
                            used_same_bar_usdt=float(used),cost_bps=float(cost),
                        )
                        expected=candidate_data[1][cost].get(key)
                        if expected is None:
                            if (decision.action!="REJECT" or
                                decision.reason not in ("duplicate_symbol","four_slot_limit")):
                                raise ValueError("CANDIDATE_OMISSION_POLICY_DRIFT")
                            candidate_portfolio_rejects+=1
                        elif expected=="SKIP":
                            if decision.action!="SKIP" or decision.reason!="vol_ge_30":
                                raise ValueError("CANDIDATE_VOL30_POLICY_DRIFT")
                            candidate_gate_skips+=1
                        elif expected=="ADMIT":
                            row=ledger_entries.get(key)
                            if row is None or decision.action!="ADMIT":
                                raise ValueError("CANDIDATE_ENTRY_POLICY_DRIFT")
                            if abs(float(row["atr"])-float(candidate["stage1_atr7_pct"]))>1e-6:
                                raise ValueError("CANDIDATE_ATR_SOURCE_DRIFT")
                            requested=min(decision.desired_notional_usdt,decision.equity_budget_usdt)
                            if abs(D(str(requested))-D(row["desired_before_liquidity"]))>D(".001"):
                                raise ValueError("CANDIDATE_SHOCK_SIZE_DRIFT")
                            expected_notional=D(str(decision.modeled_notional_usdt))
                            if sym in entry_specs:
                                spec=entry_specs[sym]
                                _,expected_notional,_=lot_quantize_v39(
                                    modeled_notional=decision.modeled_notional_usdt,
                                    entry_price=float(entry_prices[key]),
                                    contract_size=str(spec["contractSize"]),
                                    volume_step=str(spec["volUnit"]),
                                    min_volume=str(spec["minVol"]),
                                    max_volume=str(spec["maxVol"]) if spec.get("maxVol") is not None else None,
                                )
                            actual_notional=D(row["notional"])
                            if abs(expected_notional-actual_notional)>D(".001"):
                                raise ValueError("CANDIDATE_ACCOUNT_STATE_ENTRY_SIZE_DRIFT")
                            candidate_accepted.append(key)
                            accepted_candidate_decisions+=1
                            shadow_cash-=actual_notional*rate
                            shadow[sym]={
                                "time":t,"n":actual_notional,"entry":entry_prices[key],
                                "mark":entry_prices[key],
                            }
                        else:
                            raise ValueError("CANDIDATE_GATE_ACTION_INVALID")
                        candidate_decisions_checked+=1
                    if candidate_accepted!=[(r["symbol"],t) for r in entries[t]]:
                        raise ValueError("CANDIDATE_SAME_BAR_ENTRY_PRIORITY_DRIFT")
                for row in entries[t]:
                    sym = row["symbol"]
                    entry_diff = max(entry_diff, abs(equity() - D(row["equity_at_entry"])))
                    if sym in active or len(active) >= 4:
                        raise ValueError("ADMISSION_CONFLICT")
                    n = D(row["notional"])
                    price = entry_prices[(sym, t)]
                    if entry_specs is not None:
                        from app.t100_v40_admission import PriorBar, admission, lot_quantize_v39
                        history = [PriorBar(int(ts), float(hi), float(cl), float(amount)) for ts, hi, cl, amount in candles.execute(
                            "SELECT open_ts,high,close,amount FROM candles WHERE symbol=? AND interval='Min30' AND open_ts>=? AND open_ts<? ORDER BY open_ts",
                            (sym, t-49*1800, t),
                        )]
                        used = sum((D(x['notional'])*D(x['exit'])/D(x['entry']) for x in exits[t]
                                    if x['symbol']==sym and x['kind'] in ('trim','delayed_exit')), D(0))
                        decision = admission(prior_bars=history, admission_open_ts=t,
                            equity_usdt=float(equity()), marked_existing_usdt=float(sum(
                                (p['n']*p['mark']/p['entry'] for p in active.values()), D(0))),
                            stage1_atr7_pct=float(row['atr']), active_positions=len(active),
                            already_open_symbol=sym in active, used_same_bar_usdt=float(used), cost_bps=cost)
                        if decision.action != 'ADMIT':
                            raise ValueError('RECONSTRUCTED_ENTRY_NOT_ADMITTED')
                        requested = min(decision.desired_notional_usdt, decision.equity_budget_usdt)
                        if abs(D(str(requested))-D(row['desired_before_liquidity'])) > D('.001'):
                            raise ValueError('RECONSTRUCTED_REQUESTED_SIZE_MISMATCH')
                        expected = D(str(decision.modeled_notional_usdt))
                        if sym in entry_specs:
                            spec = entry_specs[sym]
                            _, expected, _ = lot_quantize_v39(modeled_notional=decision.modeled_notional_usdt,
                                entry_price=float(price), contract_size=str(spec['contractSize']),
                                volume_step=str(spec['volUnit']), min_volume=str(spec['minVol']),
                                max_volume=str(spec['maxVol']) if spec.get('maxVol') is not None else None)
                        delta = abs(expected-n)
                        sizing_max_difference = max(sizing_max_difference, delta)
                        if delta > D('.001'):
                            raise ValueError('RECONSTRUCTED_ENTRY_SIZE_MISMATCH')
                        sizing_checked += 1
                    cash -= n * rate
                    active[sym] = {"time": t, "n": n, "entry": price, "mark": price}
                eq = equity()
                peak = max(peak, eq)
                close_dd = min(close_dd, (eq / peak - 1) * 100)
                high_dd = min(high_dd, (equity(prices, 1) / peak - 1) * 100)
                for row in exits[t]:
                    if row["kind"] == "normal_exit":
                        close(row)
                for sym, p in active.items():
                    if sym in prices:
                        p["mark"] = prices[sym][2]
                eq = equity()
                peak = max(peak, eq)
                close_dd = min(close_dd, (eq / peak - 1) * 100)
            results[f"{cohort}_{cost}"] = {
                "high_drawdown_pct": str(high_dd),
                "high_drawdown_difference": str(high_dd - D(m["high_envelope_dd_pct"])),
                "close_drawdown_pct": str(close_dd),
                "close_drawdown_difference": str(close_dd - D(m["close_dd_pct"])),
                "final_equity": str(equity()),
                "equity_difference": str(equity() - D(m["final_equity"])),
                "maximum_entry_equity_difference": str(entry_diff),
                "funding_events": event_count,
                "funding_difference": str(fund - D(m["funding_net"])),
                "strategy_decisions_independently_certified": False,
                "venue_execution_certified": False,
                "live_authorization": False,
            }
            if entry_specs is not None:
                results[f"{cohort}_{cost}"].update(
                    reconstructed_entry_sizes_checked=sizing_checked,
                    maximum_entry_size_difference=str(sizing_max_difference),
                    rejected_candidates_certified=False,
                )
            if candidate_data is not None:
                if (candidate_decisions_checked!=146 or candidate_gate_skips!=24
                    or candidate_portfolio_rejects!=14 or accepted_candidate_decisions!=108):
                    raise ValueError("CANDIDATE_REPLAY_FINAL_COUNTS_DRIFT")
                results[f"{cohort}_{cost}"].update(
                    candidate_decisions_checked=candidate_decisions_checked,
                    candidate_gate_skips_checked=candidate_gate_skips,
                    candidate_occupancy_rejects_checked=candidate_portfolio_rejects,
                    candidate_admissions_and_sizes_checked=accepted_candidate_decisions,
                    candidate_decisions_given_prescribed_fills_verified=True,
                    independent_stage2_candidate_generation_certified=False,
                    independent_exit_fill_decisions_certified=False,
                    historical_point_in_time_specifications_certified=False,
                    full_strategy_decisions_independently_certified=False,
                )
            if audit_trims:
                results[f"{cohort}_{cost}"].update(
                    prior_close_trim_plans=trim_plans, observed_trim_fills_checked=trim_fills,
                    trim_fill_quantities_certified=False,
                )
    candles.close()
    funds.close()
    return results


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(
        description="Independent marked equity audit of prescribed replay trades; no decision or venue certification."
    )
    parser.add_argument("--data-root", type=Path, default=Path.home() / "trader-backtest")
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--folder", type=Path,
                       help="Original V40 output folder containing entries/exits/fills/metrics CSVs")
    group.add_argument("--baseline", type=Path)
    parser.add_argument("--filtered", type=Path,
                        help="Required if --baseline is given")
    parser.add_argument("--candles", type=Path,
                        help="Historical Min30 SQLite file, e.g. verified SSD copy")
    parser.add_argument("--funding", type=Path,
                        help="Historical funding cache SQLite file")
    parser.add_argument("--immutable-candles", action="store_true",
                        help="Only for complete dormant offline SQLite snapshots")
    parser.add_argument("--candidate-archive", type=Path,
                        help="Pinned original 146-candidate ZIP; requires --folder and --lot-snapshot")
    parser.add_argument("--lot-snapshot", type=Path,
                        help="Current-reference MEXC contract metadata JSON (NOT historical)")
    args = parser.parse_args()
    if (args.baseline is None) != (args.filtered is None):
        parser.error("--baseline and --filtered must be used together")
    folders = ({"historical": args.folder} if args.folder is not None else
               {"baseline": args.baseline, "filtered": args.filtered})
    if (args.candidate_archive is not None) != (args.lot_snapshot is not None):
        parser.error("--candidate-archive and --lot-snapshot must be used together")
    if args.candidate_archive is not None and args.folder is None:
        parser.error("candidate replay requires --folder, not baseline/filtered")
    specs=None
    if args.lot_snapshot is not None:
        payload=json.loads(args.lot_snapshot.expanduser().read_text())
        if (payload.get("NOT_HISTORICAL") is not True or
            payload.get("purpose")!="CURRENT_CONTRACT_SPEC_REFERENCE_ONLY"):
            parser.error("snapshot must explicitly be NOT_HISTORICAL current reference")
        specs=payload["specifications"]
    print(json.dumps(
        audit(args.data_root.resolve(), folders,
              candles_path=args.candles, funding_path=args.funding,
              immutable_candles=args.immutable_candles,
              entry_specs=specs,candidate_archive=args.candidate_archive),
        indent=2,
    ))
