"""Strict offline comparison of baseline versus filtered chronological V40 output.

Requires two separate original-simulator output directories each containing
metrics.csv and entries.csv (or one nested immediate directory with them).
Reconciles the baseline with the frozen V40 2000/HYBRID 100-bps archive,
then verifies every filtered entry belongs to the frozen public API whitelist.
Never asserts future/historical venue access or execution safety.
"""
from __future__ import annotations

import argparse
import csv
import json
import math
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any
from zipfile import ZipFile

from scripts.v40_api_filtered_original_runner import (
    validate_manifest,
)
from scripts.v40_backtest_api_coverage import _csv_from_zip, read_entries, scenario_label


def _frozen_row_matches(actual: dict[str, str], expected: dict[str, str]) -> bool:
    """Compare every frozen field, including chronology, size and risk metrics.

    Permit only tiny serialization noise for finite numbers. Missing columns,
    NaN/Infinity, changed text and booleans fail closed. Additional output
    diagnostics do not change the frozen comparison surface.
    """
    for field, value in expected.items():
        observed = actual.get(field)
        if observed is None:
            return False
        try:
            left, right = Decimal(observed), Decimal(value)
        except (InvalidOperation, TypeError):
            if observed != value:
                return False
        else:
            if not left.is_finite() or not right.is_finite():
                return False
            if abs(left - right) > max(Decimal("1e-8"), abs(right) * Decimal("1e-10")):
                return False
    return True


def _find_one_csv(folder: Path, filename: str) -> Path:
    if not folder.is_dir():
        raise ValueError("REPLAY_OUTPUT_NOT_DIRECTORY")
    matches = list(folder.rglob(filename))
    matches = [x for x in matches if x.is_file()]
    if len(matches) != 1:
        raise ValueError("REPLAY_OUTPUT_CSV_NOT_UNIQUE")
    if matches[0].stat().st_size > 10_000_000:
        raise ValueError("REPLAY_OUTPUT_CSV_TOO_LARGE")
    return matches[0]


def _load_rows(folder: Path, filename: str) -> list[dict[str, str]]:
    with _find_one_csv(folder, filename).open(
        "r", encoding="utf-8-sig", newline=""
    ) as source:
        rows = list(csv.DictReader(source))
    if not rows:
        raise ValueError("REPLAY_OUTPUT_CSV_EMPTY")
    return rows


def _get_scenario(folder: Path, scenario: str) -> tuple[dict[str, str], list[dict[str, str]]]:
    metrics = [r for r in _load_rows(folder, "metrics.csv")
               if r.get("scenario") == scenario]
    entries = [r for r in _load_rows(folder, "entries.csv")
               if r.get("scenario") == scenario]
    if len(metrics) != 1 or "entered" not in metrics[0] or "final_equity" not in metrics[0]:
        raise ValueError("REPLAY_METRICS_SCENARIO_UNVERIFIED")
    if len(entries) != int(metrics[0]["entered"]):
        raise ValueError("REPLAY_ENTRY_COUNT_MISMATCH")
    equity = float(metrics[0]["final_equity"])
    if not math.isfinite(equity) or equity <= 0:
        raise ValueError("REPLAY_FINAL_EQUITY_INVALID")
    return metrics[0], entries


def compare_replays(
    baseline_dir: Path, filtered_dir: Path, archive: Path, manifest: Path
) -> dict[str, Any]:
    allowed, denied = validate_manifest(manifest, archive)
    original = read_entries(archive, capital=2000, cost_bps=100, mode="HYBRID")
    scenario = scenario_label(100, "HYBRID")
    if baseline_dir.resolve() == filtered_dir.resolve():
        raise ValueError("REPLAY_OUTPUT_DIRS_IDENTICAL")
    bm, be = _get_scenario(baseline_dir, scenario)
    fm, fe = _get_scenario(filtered_dir, scenario)
    if int(bm["entered"]) != 108 or len(be) != 108:
        raise ValueError("BASELINE_ENTRY_COUNT_NOT_REPRODUCED")
    if [r.get("symbol") for r in be] != list(original.symbols):
        # Order matters: a different chronology is not the frozen baseline.
        raise ValueError("BASELINE_TRADE_ORDER_NOT_REPRODUCED")
    b_equity = float(bm["final_equity"])
    f_equity = float(fm["final_equity"])
    if not math.isclose(b_equity, original.final_equity,
                        rel_tol=1e-8, abs_tol=0.02):
        raise ValueError("BASELINE_EQUITY_NOT_REPRODUCED")
    with ZipFile(archive) as frozen:
        frozen_entries = [r for r in _csv_from_zip(frozen, "capital_2000/entries.csv")
                          if r.get("scenario") == scenario]
        frozen_metrics = [r for r in _csv_from_zip(frozen, "capital_2000/metrics.csv")
                          if r.get("scenario") == scenario]
    if not all(_frozen_row_matches(a, e) for a, e in zip(be, frozen_entries, strict=True)):
        raise ValueError("BASELINE_ENTRY_FIELDS_NOT_REPRODUCED")
    if not _frozen_row_matches(bm, frozen_metrics[0]):
        raise ValueError("BASELINE_METRIC_FIELDS_NOT_REPRODUCED")
    if any(r.get("symbol") not in allowed for r in fe):
        raise ValueError("FILTERED_ENTRY_INELIGIBLE_SYMBOL")
    if any(r.get("symbol") in denied for r in fe):
        raise ValueError("FILTERED_ENTRY_EXCLUDED_SYMBOL")
    baseline_dd = bm.get("high_envelope_dd_pct")
    filtered_dd = fm.get("high_envelope_dd_pct")
    def dd(raw: str | None) -> float | None:
        if raw is None or raw == "":
            return None
        n = float(raw)
        return round(n, 4) if math.isfinite(n) else None

    return {
        "status": "V40_CURRENT_API_FILTER_CHRONOLOGICAL_REPLAY_RECONCILED",
        "scenario": scenario,
        "original_baseline_equity_usdt": round(b_equity, 2),
        "original_baseline_multiple": round(b_equity / 2000, 4),
        "original_baseline_entries": len(be),
        "filtered_equity_usdt": round(f_equity, 2),
        "filtered_equity_multiple": round(f_equity / 2000, 4),
        "filtered_entries": len(fe),
        "filtered_unique_contracts": len({r["symbol"] for r in fe}),
        "entry_count_difference": len(fe) - len(be),
        "final_equity_difference_usdt": round(f_equity - b_equity, 2),
        "baseline_high_envelope_dd_pct": dd(baseline_dd),
        "filtered_high_envelope_dd_pct": dd(filtered_dd),
        "excluded_symbols_in_filtered_entries": 0,
        "baseline_trades_and_equity_reproduced": True,
        "baseline_all_frozen_entry_fields_reproduced": True,
        "baseline_all_frozen_metric_fields_reproduced": True,
        "historical_api_permissions_certified": False,
        "venue_execution_certified": False,
        "live_authorization": False,
        "entry_allowed": False,
        "mexc_orders_sent": False,
        "read_only": True,
    }


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--baseline-output", type=Path, required=True)
    p.add_argument("--filtered-output", type=Path, required=True)
    p.add_argument("--archive", type=Path, required=True)
    p.add_argument("--manifest", type=Path, required=True)
    a = p.parse_args()
    try:
        report = compare_replays(
            a.baseline_output.expanduser(), a.filtered_output.expanduser(),
            a.archive.expanduser(), a.manifest.expanduser(),
        )
        print(json.dumps(report, indent=2, sort_keys=True))
    except Exception as exc:
        known = {
            "REPLAY_OUTPUT_NOT_DIRECTORY", "REPLAY_OUTPUT_CSV_NOT_UNIQUE",
            "REPLAY_OUTPUT_CSV_TOO_LARGE", "REPLAY_OUTPUT_CSV_EMPTY",
            "REPLAY_METRICS_SCENARIO_UNVERIFIED", "REPLAY_ENTRY_COUNT_MISMATCH",
            "REPLAY_FINAL_EQUITY_INVALID", "REPLAY_OUTPUT_DIRS_IDENTICAL",
            "BASELINE_ENTRY_COUNT_NOT_REPRODUCED", "BASELINE_TRADE_ORDER_NOT_REPRODUCED",
            "BASELINE_EQUITY_NOT_REPRODUCED", "FILTERED_ENTRY_INELIGIBLE_SYMBOL",
            "BASELINE_ENTRY_FIELDS_NOT_REPRODUCED", "BASELINE_METRIC_FIELDS_NOT_REPRODUCED",
            "FILTERED_ENTRY_EXCLUDED_SYMBOL", "FROZEN_ARCHIVE_SHA_MISMATCH",
            "FILTER_MANIFEST_NOT_VERIFIED", "FILTER_COHORT_NOT_VERIFIED",
            "CANDIDATE_SOURCE_TOO_LARGE", "FILTER_COHORT_ARCHIVE_MISMATCH",
        }
        code = str(exc) if str(exc) in known else "REPLAY_REPORT_INPUT_UNVERIFIED"
        print(json.dumps({
            "status": "V40_API_FILTERED_REPLAY_COMPARISON_HALTED",
            "failure_code": code, "live_authorization": False,
            "mexc_orders_sent": False,
        }, indent=2, sort_keys=True))
        raise SystemExit(2) from None


if __name__ == "__main__":
    main()
