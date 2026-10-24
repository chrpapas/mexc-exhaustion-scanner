#!/usr/bin/env python3
"""Read-only V40 admission parity verifier for a locally exported ZIP.

Uses the source-grounded implementation app.t100_v40_admission. Does not
extract the ZIP, connect to MEXC, read trading secrets, place orders, or
recompute backtested equity. Intended to run at the repository root:

    PYTHONPATH=. python scripts/v40_historical_parity_audit.py /path/to/v40_parity_for_live.zip
"""
from __future__ import annotations

import argparse
import csv
from datetime import datetime
import io
import json
import math
from collections import Counter, defaultdict
from pathlib import Path
from zipfile import ZipFile

from app.t100_v40_admission import PriorBar, causal_49_bar_features
from app import t100_v40_admission as live_policy

CAPITALS = (2000, 10000, 100000)
COSTS = (100, 200)
LOT_MODES = ('REFERENCE', 'HYBRID', 'KNOWN_ONLY')
FEATURE_TOLERANCE = 0.000003
NOTIONAL_TOLERANCE = 0.000003


def records(z: ZipFile, name: str) -> list[dict[str, str]]:
    if name not in z.namelist():
        raise ValueError(f'archive is missing {name}')
    # The parity archive contains only text CSV and JSON files; no extraction.
    return list(csv.DictReader(io.StringIO(z.read(name).decode('utf-8'))))


def label(cost: int, mode: str) -> str:
    base = f'V32_VOL30_SKIP_H4_S{cost}_CAP5'
    return base if mode == 'REFERENCE' else f'{base}_V39_{mode}'


def approx(a: float, b: float, tol: float = FEATURE_TOLERANCE) -> bool:
    return math.isfinite(a) and math.isfinite(b) and abs(a-b) <= max(tol, 2.e-10 * abs(b))


def key_for(c: dict[str, str]) -> tuple[str, int]:
    return c['symbol'], int(c['entry_ts'])


def run(path: Path) -> dict:
    with ZipFile(path) as z:
        manifest = json.loads(z.read('manifest.json').decode())
        if manifest.get('candidate_count') != 146 or manifest.get('preceding_candles_exported') != 7154:
            raise AssertionError('FROZEN_DATASET_MANIFEST_MISMATCH')
        if manifest.get('type') != 'READ_ONLY_SOURCE_PARITY_SAMPLE':
            raise AssertionError('UNEXPECTED_ARCHIVE_TYPE')
        candidates = records(z, 'candidates.csv')
        candles = records(z, 'prior49_bars.csv')
        if len(candidates) != 146 or len(candles) != 146*49:
            raise AssertionError('EXPECTED_146_CANDIDATES_AND_7154_PRIOR_CANDLES')
        candidate_by_key = {key_for(c): c for c in candidates}
        if len(candidate_by_key) != len(candidates):
            raise AssertionError('DUPLICATE_CANDIDATE_KEY')
        bars = defaultdict(list)
        for r in candles:
            k = (r['signal_symbol'], int(r['admission_ts']))
            if k not in candidate_by_key:
                raise AssertionError(f'ORPHAN_PRIOR_BAR_{k}')
            bars[k].append(PriorBar(int(r['open_ts']), float(r['high']),
                                    float(r['close']), float(r['amount'])))
        features = {}
        for k in candidate_by_key:
            if len(bars[k]) != 49:
                raise AssertionError(f'INCORRECT_PRIOR_BAR_COUNT_{k}')
            f = causal_49_bar_features(bars[k], admit_open_ts=k[1])
            if f is None:
                raise AssertionError(f'INCOMPLETE_CAUSAL_FEATURES_{k}')
            features[k] = f

        checked = 0
        feature_comparisons = 0
        ref_notionals = 0
        direct_policy_notionals = 0
        cost_mode_entry_counts = Counter()
        results = {}
        for capital in CAPITALS:
            gate_rows = records(z, f'capital_{capital}/gate_decisions.csv')
            entry_rows = records(z, f'capital_{capital}/entries.csv')
            metrics = records(z, f'capital_{capital}/metrics.csv')
            summary = {'costs': {}, 'baseline_hybrid_final_equity_usdt': None}
            for cost in COSTS:
                cost_report = {}
                for mode in LOT_MODES:
                    scenario = label(cost, mode)
                    g = [r for r in gate_rows if r['scenario'] == scenario]
                    es = [r for r in entry_rows if r['scenario'] == scenario]
                    ms = [r for r in metrics if r['scenario'] == scenario]
                    if len(ms) != 1:
                        raise AssertionError(f'MISSING_METRIC_{capital}_{scenario}')
                    m = ms[0]
                    accepted_gates = set()
                    gate_key_set = set()
                    actions = Counter()
                    for r in g:
                        k = (r['symbol'], int(datetime.fromisoformat(r['time'].replace('Z','+00:00')).timestamp()))
                        if k in gate_key_set or k not in features:
                            raise AssertionError(f'DUPLICATE_OR_UNKNOWN_GATE_{capital}_{scenario}_{k}')
                        gate_key_set.add(k)
                        volatility = features[k][0]
                        expected = 'SKIP' if volatility >= 30.0 else 'ADMIT'
                        if r['action'] != expected:
                            raise AssertionError(f'GATE_MISMATCH_{capital}_{scenario}_{k}_{r["action"]}_vs_{expected}')
                        if not approx(float(r['pre24_vol_pct']), volatility):
                            raise AssertionError(f'VOLATILITY_MISMATCH_{capital}_{scenario}_{k}')
                        if expected == 'ADMIT':
                            accepted_gates.add(k)
                        actions[expected] += 1
                        checked += 1
                    entry_key_set = set()
                    for r in es:
                        k = (r['symbol'], int(datetime.fromisoformat(r['time'].replace('Z','+00:00')).timestamp()))
                        if k in entry_key_set or k not in accepted_gates:
                            raise AssertionError(f'ENTRY_WITHOUT_ADMIT_GATE_{capital}_{scenario}_{k}')
                        entry_key_set.add(k)
                        f = features[k]
                        atr = float(candidate_by_key[k]['stage1_atr7_pct'])
                        eq = float(r['equity_at_entry'])
                        desired_target = eq * (0.625 if atr >= 5.7 else 0.25)
                        observed_target = eq * float(r['desired_allocation_pct']) / 100.
                        expected_exit_capacity = .05 * f[2] * 4 * .5 / 1.75
                        for name, actual, expected in (
                            ('p25', float(r['p25_turnover']), f[1]),
                            ('p10', float(r['p10_turnover']), f[2]),
                            ('exit_capacity', float(r['exit_capacity_limit']), expected_exit_capacity),
                            ('target', observed_target, desired_target)):
                            if not approx(actual, expected):
                                raise AssertionError(f'ENTRY_DIAGNOSTIC_MISMATCH_{name}_{capital}_{scenario}_{k}: {actual} vs {expected}')
                            feature_comparisons += 1
                        actual = float(r['notional'])
                        requested = float(r['desired_before_liquidity'])
                        remaining_quota = float(r['prior_turnover_quota_usdt'])
                        capacity_ceiling = min(requested, remaining_quota, .05*f[1], expected_exit_capacity)
                        if mode == 'REFERENCE':
                            if not approx(actual, capacity_ceiling, NOTIONAL_TOLERANCE):
                                raise AssertionError(f'REFERENCE_NOTIONAL_MISMATCH_{capital}_{cost}_{k}: {actual} vs {capacity_ceiling}')
                            ref_notionals += 1
                            # Independently exercise the actual pure live admission
                            # function in the exported no-shock-clip cases.
                            # An occupied/margin-constrained portfolio remains untested.
                            if approx(requested, desired_target):
                                call = getattr(live_policy, 'admission', None) or live_policy.v40_admission
                                decision = call(
                                    prior_bars=bars[k], admission_open_ts=k[1],
                                    equity_usdt=eq, marked_existing_usdt=0.,
                                    stage1_atr7_pct=atr, active_positions=0,
                                    already_open_symbol=False,
                                    used_same_bar_usdt=max(0., .05*f[3]-remaining_quota),
                                    cost_bps=float(cost))
                                if decision.action != 'ADMIT' or not approx(
                                        decision.modeled_notional_usdt, actual, NOTIONAL_TOLERANCE):
                                    raise AssertionError(f'DIRECT_POLICY_NOTIONAL_MISMATCH_{capital}_{cost}_{k}')
                                direct_policy_notionals += 1
                        elif actual > capacity_ceiling + max(NOTIONAL_TOLERANCE, 2.e-10 * capacity_ceiling):
                            raise AssertionError(f'LOT_MODE_OVERSIZE_{capital}_{scenario}_{k}')
                    if len(es) != int(m['entered']):
                        raise AssertionError(f'ENTRY_METRIC_COUNT_MISMATCH_{capital}_{scenario}')
                    cost_mode_entry_counts[(capital, cost, mode)] = len(es)
                    cost_report[mode] = {
                        'gate_evaluated': len(g), 'gate_skipped': actions['SKIP'],
                        'gate_admitted': actions['ADMIT'], 'actual_entries': len(es),
                    }
                summary['costs'][str(cost)] = cost_report
            target_metrics = next(r for r in metrics if r['scenario'] == label(100,'HYBRID'))
            summary['baseline_hybrid_final_equity_usdt'] = float(target_metrics['final_equity'])
            summary['baseline_hybrid_max_dd_pct'] = float(target_metrics['high_envelope_dd_pct'])
            summary['historical_venue_executable_certified'] = target_metrics['historical_venue_executable_certified']
            results[str(capital)] = summary
        return {
            'status': 'V40_ENTRY_FILTER_AND_REFERENCE_CAPACITY_PARITY_PASS',
            'scope': 'historical gate decisions, causal volatility/turnover, and REFERENCE entries; not full replay, not actual fills',
            'archive_manifest': manifest,
            'candidates': len(candidates), 'prior_candles': len(candles),
            'gate_decisions_checked': checked,
            'entry_diagnostic_comparisons_checked': feature_comparisons,
            'reference_entry_notionals_exact': ref_notionals,
            'direct_policy_reference_notionals_exact': direct_policy_notionals,
            'discrepancies': 0,
            'tested_capitals': results,
            'limitations': [
                'HYBRID/KNOWN_ONLY lot rounding cannot be exactly reproduced without the contract specification snapshot',
                'The direct-policy comparison is restricted to exported entries where the shock budget was not binding',
                'Existing marked portfolio exposure, fees, funding and exits were not independently replayed',
                'V40 liquidity capacity is a historical candle turnover proxy, not live executable MEXC depth',
                'No live order, exchange stop or live fill is certified',
                'Gates are absent for signals rejected earlier by the original portfolio occupancy/duplicate logic',
            ],
        }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('archive', type=Path)
    parser.add_argument('--output', type=Path)
    args = parser.parse_args()
    report = run(args.archive)
    rendered = json.dumps(report, indent=2, sort_keys=True)
    if args.output:
        args.output.write_text(rendered+'\n', encoding='utf-8')
    print(rendered)


if __name__ == '__main__':
    main()