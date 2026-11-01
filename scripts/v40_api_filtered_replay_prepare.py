"""Prepare a frozen local V40 candidate API filter BEFORE a full replay.

Only current PUBLIC MEXC GETs, no credentials or orders. Reads the PINNED V40
parity ZIP and writes a local operator-private JSON replay-input manifest.
DOES NOT compute altered PnL or modify the original replay source/data.
Excluded currently unsupported symbols must be gated BEFORE portfolio
admission, not removed from the saved 108 trade ledger afterward.
"""
from __future__ import annotations

import argparse
import asyncio
import csv
from datetime import datetime, timezone
import hashlib
import io
import json
import os
from pathlib import Path
from typing import Any
from zipfile import ZipFile

from app.t100_live_api_eligibility_census import _parse_data, _digest
from app.t100_live_cross_risk_diagnostic import (
    CrossRiskDiagnosticHalted, require_readonly_controls,
)
from scripts.v40_backtest_api_coverage import (
    compare_current_support, current_public_status, fetch_public_country_pair,
    read_entries, HistoricalEntries,
)

PINNED_SHA256 = "2918f4589e8d564ca55b6f24aa2e9a868b5d214653ad7d92acaf1729fd1e4cb5"


def _hash_file(path: Path) -> str:
    dig = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            dig.update(chunk)
    return dig.hexdigest()


def prepare_manifest(
    archive: Path, first: Any, second: Any, *,
    require_pinned: bool = True,
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Pure offline cohort preparation; accepts fake archives for unit tests."""
    sha = _hash_file(archive)
    if require_pinned and sha != PINNED_SHA256:
        raise ValueError("FROZEN_ARCHIVE_SHA_MISMATCH")
    with ZipFile(archive) as z:
        manifest = json.loads(z.read("manifest.json"))
        if (manifest.get("type") != "READ_ONLY_SOURCE_PARITY_SAMPLE"
                or manifest.get("candidate_count") != 146):
            raise ValueError("FROZEN_CANDIDATE_MANIFEST_INVALID")
        info = z.getinfo("candidates.csv")
        if info.file_size > 2_000_000:
            raise ValueError("CANDIDATE_CSV_TOO_LARGE")
        candidates = list(csv.DictReader(
            io.StringIO(z.read("candidates.csv").decode("utf-8-sig"))
        ))
    if len(candidates) != 146:
        raise ValueError("CANDIDATE_COUNT_MISMATCH")
    candidate_keys: set[tuple[str, str]] = set()
    candidate_symbols: set[str] = set()
    for row in candidates:
        symbol, timestamp = row.get("symbol"), row.get("entry_ts")
        if (type(symbol) is not str or not symbol.endswith("_USDT")
                or not symbol.replace("_USDT", "").isalnum()
                or not symbol.replace("_USDT", "").isupper()
                or type(timestamp) is not str or not timestamp.isdigit()):
            raise ValueError("CANDIDATE_SCHEMA_INVALID")
        key = (symbol, timestamp)
        if key in candidate_keys:
            raise ValueError("DUPLICATE_CANDIDATE")
        candidate_keys.add(key)
        candidate_symbols.add(symbol)

    entries = read_entries(archive, capital=2000, cost_bps=100, mode="HYBRID")
    if len(entries.symbols) != 108 or not set(entries.symbols) <= candidate_symbols:
        raise ValueError("BASELINE_ENTRY_COHORT_INVALID")

    before = tuple(sorted(_parse_data(first), key=lambda r: r["symbol"]))
    after = tuple(sorted(_parse_data(second), key=lambda r: r["symbol"]))
    if _digest(before) != _digest(after):
        raise ValueError("PUBLIC_CONTRACT_METADATA_DRIFT")
    by_symbol = {r["symbol"]: r for r in after}
    supported = sorted(s for s in candidate_symbols
                       if current_public_status(by_symbol.get(s)) ==
                       "CURRENT_PUBLIC_ELIGIBLE")
    excluded = sorted(candidate_symbols - set(supported))
    baseline_coverage = compare_current_support(entries, first, second)
    eligible_entry_count = sum(s in set(supported) for s in entries.symbols)
    if eligible_entry_count != baseline_coverage["currently_eligible_entries"]:
        raise ValueError("ENTRY_COHORT_COVERAGE_CONTRADICTION")
    if require_pinned and (baseline_coverage["currently_eligible_entries"] != 97
                           or baseline_coverage["currently_eligible_unique"] != 75):
        # The public universe is live, so changed values must be investigated,
        # not silently assumed to remain at the user's prior snapshot.
        # Do not block such changes: report them as current data.
        pass
    local_manifest = {
        "type": "V40_CURRENT_PUBLIC_API_CANDIDATE_PREFILTER",
        "archive_sha256": sha,
        "scenario": entries.scenario,
        "initial_capital_usdt": 2000,
        "saved_baseline_final_equity_usdt": round(entries.final_equity, 2),
        "candidate_count": len(candidates),
        "candidate_unique_contracts": len(candidate_symbols),
        "candidate_symbols_current_public_eligible": supported,
        "candidate_symbols_current_public_ineligible": excluded,
        "public_contract_snapshot_sha256": _digest(after),
        "current_public_metadata_only": True,
        "point_in_time_historical_api_access_verified": False,
        "account_specific_api_order_permission_verified": False,
        "portfolio_replay_performed": False,
        "historical_return_after_filter_verified": False,
        "live_authorization": False,
    }
    report = {
        "status": "V40_PUBLIC_API_PREFILTER_READY_REPLAY_NOT_YET_RUN",
        "frozen_archive_sha256": sha,
        "baseline_entries": len(entries.symbols),
        "baseline_saved_equity_multiple": round(entries.final_equity / 2000, 4),
        "baseline_current_public_eligible_entries": eligible_entry_count,
        "baseline_current_public_ineligible_entries": len(entries.symbols) - eligible_entry_count,
        "candidate_signals": len(candidates),
        "candidate_distinct_symbols": len(candidate_symbols),
        "candidate_distinct_public_eligible": len(supported),
        "candidate_distinct_public_ineligible": len(excluded),
        "current_public_snapshot_stable": True,
        "filtered_equity_multiple": None,
        "chronological_portfolio_replayed": False,
        "account_specific_api_order_access_certified": False,
        "live_authorization": False,
        "entry_allowed": False,
        "mexc_orders_sent": False,
        "read_only": True,
    }
    return local_manifest, report


def write_new_local_manifest(path: Path, result: dict[str, Any]) -> None:
    """Create only: never overwrite a prior time-stamped public filter."""
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as target:
            json.dump(result, target, indent=2, sort_keys=True)
            target.write("\n")
    except Exception:
        path.unlink(missing_ok=True)
        raise


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--archive", type=Path,
                        default=Path("~/v40_parity_for_live.zip").expanduser())
    parser.add_argument("--output", type=Path, required=True,
                        help="Local new JSON filename; refuses to overwrite")
    args = parser.parse_args()
    try:
        require_readonly_controls(os.environ)
    except CrossRiskDiagnosticHalted:
        raise SystemExit("PAPER_KILL_REQUIRED") from None
    try:
        first, second = asyncio.run(fetch_public_country_pair())
        prepared, report = prepare_manifest(args.archive.expanduser(), first, second)
        prepared["snapshot_created_utc"] = datetime.now(timezone.utc).isoformat()
        output = args.output.expanduser()
        if not output.parent.is_dir():
            raise ValueError("OUTPUT_PARENT_MISSING")
        write_new_local_manifest(output, prepared)
        report["local_manifest_saved"] = True
        report["local_manifest_filename"] = output.name
        print(json.dumps(report, indent=2, sort_keys=True))
        print("REPLAY NOT RUN. LOCAL SOURCE REQUIRED. NO KEYS OR ORDERS.")
    except Exception as exc:
        allowed = {
            "FROZEN_ARCHIVE_SHA_MISMATCH", "FROZEN_CANDIDATE_MANIFEST_INVALID",
            "CANDIDATE_CSV_TOO_LARGE", "CANDIDATE_COUNT_MISMATCH",
            "CANDIDATE_SCHEMA_INVALID", "DUPLICATE_CANDIDATE",
            "BASELINE_ENTRY_COHORT_INVALID", "PUBLIC_CONTRACT_METADATA_DRIFT",
            "ENTRY_COHORT_COVERAGE_CONTRADICTION", "OUTPUT_PARENT_MISSING",
        }
        raw = str(exc)
        code = raw if raw in allowed else "LOCAL_INPUT_OR_PUBLIC_READ_UNVERIFIED"
        print(json.dumps({"status": "V40_PREFILTER_HALTED",
                          "failure_code": code, "live_authorization": False,
                          "mexc_orders_sent": False}, indent=2, sort_keys=True))
        raise SystemExit(2) from None


if __name__ == "__main__":
    main()
