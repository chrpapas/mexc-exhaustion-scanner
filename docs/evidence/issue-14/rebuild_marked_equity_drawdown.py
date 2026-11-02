"""Research trace audit. Uses fixed exported trades, never executes the strategy or orders."""

import csv, json, sqlite3
from collections import defaultdict
from datetime import datetime
from decimal import Decimal as D
from pathlib import Path

start = int(datetime.fromisoformat("2023-02-03T02:30:00+00:00").timestamp())
end = int(datetime.fromisoformat("2026-09-25T08:30:00+00:00").timestamp())


def ts(s):
    return int(datetime.fromisoformat(s).timestamp())


def conn(path, immutable=False):
    if immutable and Path(str(path) + "-wal").exists():
        raise ValueError("UNSNAPSHOTTED_WAL")
    c = sqlite3.connect(path.as_uri() + "?mode=ro" + ("&immutable=1" if immutable else ""), uri=True)
    c.execute("PRAGMA query_only=ON")
    c.execute("BEGIN")
    return c


def audit(root, folders, start=start, end=end, audit_trims=False, entry_specs=None):
    candles = conn(root / "historical-market-data-min30-v1/historical-market-data-min30-v1.sqlite")
    funds = conn(root / "mexc-certification-cache-v1.sqlite", True)
    results = {}
    for cohort in ("baseline", "filtered"):
        folder = folders[cohort]
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
                syms = set(active) | {r["symbol"] for r in entries[t]}
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
    parser.add_argument("--data-root", type=Path, required=True)
    parser.add_argument("--baseline", type=Path, required=True)
    parser.add_argument("--filtered", type=Path, required=True)
    args = parser.parse_args()
    print(
        json.dumps(
            audit(args.data_root.resolve(), {"baseline": args.baseline, "filtered": args.filtered}), indent=2
        )
    )
