"""Independent candle exit-trigger audit of prescribed entries and fill sizes.

No simulator imports, exchange client, or execution authorization. Opening
trim decisions and capacity-limited quantities remain prescribed inputs.
"""
from __future__ import annotations

import argparse
import csv
import json
import sqlite3
from collections import defaultdict
from datetime import datetime
from decimal import Decimal as D
from pathlib import Path


def timestamp(value):
    parsed = datetime.fromisoformat(value)
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError("NAIVE_AUDIT_TIMESTAMP")
    return int(parsed.timestamp())


def audit_position(entry_price, notional, candles, exits, *, start=None, end=None):
    """Candles are ordered (timestamp, open, high, low, close) tuples."""
    entry_price, remaining = D(str(entry_price)), D(str(notional))
    if not entry_price.is_finite() or entry_price <= 0 or not remaining.is_finite() or remaining <= 0:
        raise ValueError("INVALID_ENTRY")
    armed = False
    best = D(0)
    pending = False
    by_time = defaultdict(list)
    for row in exits:
        by_time[timestamp(row['exit_time'])].append(row)
    counts = {'candles': 0, 'triggers': 0, 'normal_fills': 0, 'delayed_fills': 0, 'trims': 0}

    def consume(row, expected_price, expected_reason):
        nonlocal remaining
        price, amount = D(row['exit']), D(row['notional'])
        if not price.is_finite() or abs(price-expected_price) > max(D('1e-10'), abs(expected_price)*D('1e-10')):
            raise ValueError('EXIT_PRICE_MISMATCH')
        if row['reason'] not in (expected_reason, expected_reason+'_partial'):
            raise ValueError('EXIT_REASON_MISMATCH')
        if not amount.is_finite() or amount <= 0 or amount > remaining+D('.001'):
            raise ValueError('EXIT_QUANTITY_INVALID')
        remaining -= amount

    previous = None
    for t, op, hi, lo, cl in candles:
        if previous is None and start is not None and t != start:
            raise ValueError("ENTRY_CANDLE_MISSING")
        if previous is not None and t != previous+1800:
            raise ValueError('CANDLE_GAP_OR_DUPLICATE')
        previous = t
        op, hi, lo, cl = (D(str(v)) for v in (op, hi, lo, cl))
        if not all(v.is_finite() for v in (op, hi, lo, cl)) or not (0 < lo <= min(op, cl) <= max(op, cl) <= hi):
            raise ValueError('INVALID_CANDLE')
        counts['candles'] += 1
        rows = by_time.pop(t, [])
        opening = [r for r in rows if r['kind'] != 'normal_exit']
        for row in opening:
            if row['kind'] == 'delayed_exit':
                if not pending:
                    raise ValueError('DELAYED_WITHOUT_PRIOR_TRIGGER')
                consume(row, op, 'delayed_forced_exit')
                counts['delayed_fills'] += 1
            elif row['kind'] == 'trim':
                consume(row, op, 'risk_trim')
                counts['trims'] += 1
            else:
                raise ValueError('UNKNOWN_EXIT_KIND')
        normal = [r for r in rows if r['kind'] == 'normal_exit']
        if remaining <= D('.001'):
            pending = False
            if normal:
                raise ValueError('NORMAL_AFTER_FULL_OPENING_CLOSE')
            break
        trigger = entry_price * (1-max(D(0), best-D('.5'))/100) if armed else entry_price*D('1.75')
        hit = not pending and hi >= trigger
        if normal and not hit:
            raise ValueError('NORMAL_WITHOUT_TRIGGER')
        if hit:
            counts['triggers'] += 1
            for row in normal:
                consume(row, max(op, trigger), 'trail' if armed else 'pretrail_stop')
                counts['normal_fills'] += 1
            pending = remaining > D('.001')
        elif not pending:
            profit = (1-lo/entry_price)*100
            if profit >= 14 or armed:
                armed = True
                best = max(best, profit)
        if remaining <= D('.001'):
            break
    if previous is None or (remaining > D('.001') and end is not None and previous != end):
        raise ValueError('INCOMPLETE_CANDLE_WINDOW')
    if by_time:
        raise ValueError('EXITS_OUTSIDE_OBSERVED_LIFECYCLE')
    return dict(counts, pending=pending, remaining_notional=str(max(D(0), remaining)))


def audit(folder, candles_path, end):
    def read(name):
        with (folder/(name+'.csv')).open() as handle:
            return list(csv.DictReader(handle))
    entries, exits, fills = (read(n) for n in ('entries', 'exits', 'fills'))
    db = sqlite3.connect(candles_path.resolve().as_uri()+'?mode=ro', uri=True)
    db.execute('PRAGMA query_only=ON')
    db.execute('BEGIN')
    results = {}
    try:
        for cost in (100, 200):
            scenario = f'V32_VOL30_SKIP_H4_S{cost}_CAP5_V39_HYBRID'
            reports = []
            for e in (x for x in entries if x['scenario'] == scenario):
                relevant = [x for x in exits if x['scenario'] == scenario and x['symbol'] == e['symbol'] and x['entry_time'] == e['time']]
                origins = [x for x in fills if x['scenario'] == scenario and x['symbol'] == e['symbol'] and x['time'] == e['time'] and x['kind'] == 'entry']
                if len(origins) != 1:
                    raise ValueError('ENTRY_FILL_NOT_UNIQUE')
                bars = db.execute("SELECT open_ts,open,high,low,close FROM candles WHERE symbol=? AND interval='Min30' AND open_ts>=? AND open_ts<=? ORDER BY open_ts", (e['symbol'], timestamp(e['time']), end))
                reports.append(audit_position(origins[0]['price'], e['notional'], bars, relevant, start=timestamp(e['time']), end=end))
            if not reports:
                raise ValueError('EMPTY_SCENARIO')
            results[str(cost)] = {
                'positions': len(reports),
                **{k: sum(x[k] for x in reports) for k in ('candles','triggers','normal_fills','delayed_fills','trims')},
                'pending_positions': sum(x['pending'] for x in reports),
                'capacity_quantities_certified': False,
                'trim_decisions_certified': False,
                'full_strategy_parity_certified': False,
                'live_authorization': False,
            }
    finally:
        db.close()
    return results


if __name__ == '__main__':
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--folder', type=Path, required=True)
    p.add_argument('--candles', type=Path, required=True)
    p.add_argument('--end', default='2026-09-25T08:30:00+00:00')
    args = p.parse_args()
    print(json.dumps(audit(args.folder, args.candles, timestamp(args.end)), indent=2))
