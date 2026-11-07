"""Independent CSV arithmetic/occupancy audit; no simulator imports or network.

The frozen model charges 8 bps fees plus the scenario's per-side slippage.
CSV notionals have six decimal places, so reconciliation allows 0.001 USDT.
This does not independently certify funding, final marks, drawdown or fills.
"""
from __future__ import annotations

import argparse
import csv
import json
from datetime import datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path

D = Decimal
TOLERANCE = D('0.001')


def number(value):
    try:
        result = D(value)
    except (InvalidOperation, TypeError, ValueError):
        raise ValueError('INVALID_AUDIT_NUMBER') from None
    if not result.is_finite():
        raise ValueError('INVALID_AUDIT_NUMBER')
    return result


def timestamp(value):
    result = datetime.fromisoformat(value)
    if result.tzinfo is None or result.utcoffset() is None:
        raise ValueError('NAIVE_AUDIT_TIMESTAMP')
    return result


def audit_rows(entries, exits, fills, metric, cost_bps):
    if cost_bps not in (100, 200):
        raise ValueError('UNSUPPORTED_COST')
    if len(entries) != int(metric['entered']) or not entries:
        raise ValueError('ENTRY_COUNT_MISMATCH')
    rate = D('0.0008') + D(cost_bps) / 10000
    fees = D(0)
    for row in fills:
        value = number(row['filled_usdt'])
        if value <= 0:
            raise ValueError('NONPOSITIVE_FILL')
        fees += value * rate
    if not fills or abs(fees - number(metric['fee_and_slip'])) > TOLERANCE:
        raise ValueError('FEE_AND_SLIPPAGE_MISMATCH')
    events = []
    for row in entries:
        events.append((timestamp(row['time']), 1, row))
    for row in exits:
        entry_time, exit_time = timestamp(row['entry_time']), timestamp(row['exit_time'])
        kind = row['kind']
        if kind not in ('trim', 'delayed_exit', 'normal_exit'):
            raise ValueError('UNKNOWN_EXIT_PHASE')
        if exit_time < entry_time or (exit_time == entry_time and kind != 'normal_exit'):
            raise ValueError('EXIT_NOT_AFTER_ENTRY')
        notional, entry, exit_price = (number(row[k]) for k in ('notional', 'entry', 'exit'))
        if min(notional, entry, exit_price) <= 0:
            raise ValueError('NONPOSITIVE_EXIT_INPUT')
        computed = notional * (1 - exit_price / entry) - notional * exit_price / entry * rate
        if abs(computed - number(row['pnl_net_before_funding'])) > TOLERANCE:
            raise ValueError('EXIT_PNL_MISMATCH')
        events.append((exit_time, 2 if kind == 'normal_exit' else 0, row))
    # Funding then opening trims/delayed exits, admission, then normal exits.
    # Normal exits must not free slots prematurely for same-bar admission.
    events.sort(key=lambda event: (event[0], event[1]))
    active = {}
    peak = 0
    for _, kind, row in events:
        symbol = row['symbol']
        if kind == 1:
            if symbol in active:
                raise ValueError('DUPLICATE_OPEN_SYMBOL')
            amount = number(row['notional'])
            if amount <= 0:
                raise ValueError('NONPOSITIVE_ENTRY')
            active[symbol] = (timestamp(row['time']), amount)
            peak = max(peak, len(active))
            if peak > 4:
                raise ValueError('FOUR_SLOT_LIMIT_EXCEEDED')
        else:
            if symbol not in active or active[symbol][0] != timestamp(row['entry_time']):
                raise ValueError('EXIT_ORIGIN_MISMATCH')
            origin, remaining = active[symbol]
            remaining -= number(row['notional'])
            if remaining < -TOLERANCE:
                raise ValueError('EXIT_EXCEEDS_OPEN_NOTIONAL')
            if abs(remaining) <= TOLERANCE:
                del active[symbol]
            else:
                active[symbol] = (origin, remaining)
    if len(active) != int(metric['open_positions']):
        raise ValueError('FINAL_POSITION_COUNT_MISMATCH')
    return {'status': 'EXPORTED_CASHFLOW_AND_OCCUPANCY_AUDIT_PASS',
            'entries_checked': len(entries), 'exit_cashflows_checked': len(exits),
            'fills_checked': len(fills), 'peak_slots': peak, 'final_open_slots': len(active),
            'recomputed_fee_and_slippage': str(fees),
            'funding_independently_verified': False, 'final_equity_independently_verified': False,
            'drawdown_independently_verified': False, 'venue_execution_certified': False,
            'entry_allowed': False, 'live_authorization': False, 'mexc_orders_sent': False}


def audit_directory(folder, cost_bps):
    scenario = f'V32_VOL30_SKIP_H4_S{cost_bps}_CAP5_V39_HYBRID'
    def rows(name):
        with (folder / name).open(newline='', encoding='utf-8-sig') as source:
            return [r for r in csv.DictReader(source) if r['scenario'] == scenario]
    metrics = rows('metrics.csv')
    if len(metrics) != 1:
        raise ValueError('METRICS_NOT_UNIQUE')
    return audit_rows(rows('entries.csv'), rows('exits.csv'), rows('fills.csv'), metrics[0], cost_bps)


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('folder', type=Path)
    args = parser.parse_args()
    try:
        print(json.dumps({str(cost): audit_directory(args.folder, cost) for cost in (100, 200)}, indent=2))
    except Exception:
        print('{"status":"CASHFLOW_AUDIT_FAILED","live_authorization":false}')
        raise SystemExit(2) from None
