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


def audit(root, folders, start=start, end=end):
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
                for sym, p in active.items():
                    if sym in prices:
                        p["mark"] = prices[sym][0]
                for row in exits[t]:
                    if row["kind"] in ("trim", "delayed_exit"):
                        close(row)
                for row in entries[t]:
                    sym = row["symbol"]
                    entry_diff = max(entry_diff, abs(equity() - D(row["equity_at_entry"])))
                    if sym in active or len(active) >= 4:
                        raise ValueError("ADMISSION_CONFLICT")
                    n = D(row["notional"])
                    price = entry_prices[(sym, t)]
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
