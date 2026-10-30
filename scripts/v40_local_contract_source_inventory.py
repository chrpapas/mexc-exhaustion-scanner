#!/usr/bin/env python3
"""Offline discovery of candidate MEXC contract-spec sources for frozen V40.

Reads an operator's LOCAL files only. Does not authenticate Wayback captures,
assume contract metadata persisted between two dates, read credentials, issue
network requests, or grant a historical capacity / trading certification.

The output reports schema field names, counts, SHA-256 and aggregate symbol
overlap with the pinned V40 entry export; never prints individual trade
symbols, prices, wallet balances or source record contents.
"""
from __future__ import annotations

import argparse
import csv
from datetime import datetime, timezone
import hashlib
import io
import json
from pathlib import Path
import re
from typing import Any

from scripts.v40_historical_capacity_audit import (
    PINNED_FROZEN_ARCHIVE_SHA256, load_entry_ledger,
)

# Relative paths only. None is used as a snapshot source without verification.
SOURCE_PATHS = (
    "t100-mac-transfer/t100_contract_tier_certification_v1/audit_20261016_131546/trade_current_spec_checks.csv",
    "t100-frozen-capacity-sensitivity-v3/public_contract_snapshot.json",
    "t100-full-opportunity-dual-fill-v2/specs.json",
    "t100-full-opportunity-research-v1/specs.json",
    "t100-cross-margin-solvency-v36/mexc_current_public_contract_specs.json",
    "stage2-t100-175-mexc-certification-v1/current_contract_spec_snapshot.csv",
    "t100-gate-5x-current-tiers-v1/gate_public_risk_tiers_raw.json",
    "_cleanup-quarantine-2026-09-30/wayback-historical-universe-v1/snapshot_20220927012203_contracts.json",
    "_cleanup-quarantine-2026-09-30/wayback-historical-universe-v1/snapshot_20251111153736_contracts.json",
    "_cleanup-quarantine-2026-09-30/mexc-historical-coverage-discovery-v1/contract_detail_raw.json",
    "_cleanup-quarantine-2026-09-30/historical-universe-source-probe-v2/wayback_contracts_20251111153736.json",
    "_cleanup-quarantine-2026-09-30/historical-universe-source-probe-v2/wayback_contracts_20220927012203.json",
    "_cleanup-quarantine-2026-09-30/historical-universe-reconstruction-v1_1/recovered_hidden_contracts.csv",
    "t100-capacity-audit/trade_current_spec_checks.csv",
    "multi-venue-capacity-full/venue_contracts.csv",
)

MAX_SOURCE_BYTES = 100_000_000
MAX_SCAN_OBJECTS = 300_000
MAX_CSV_ROWS = 250_000
SYM = re.compile(r"^[A-Z0-9]+_USDT$")
STAMP = re.compile(r"(?<!\d)(20\d{12})(?!\d)")
_CAPTURE_TOKENS = ("observed_at", "captured_at", "fetched_at", "retrieved_at",
                   "snapshot_at", "capture_ts", "crawl_date", "archived_at",
                   "observation_time", "as_of")
_FIELDS_LIMIT = 40


def _hash_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1048576), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _hint(relative: str) -> str | None:
    match = STAMP.search(relative)
    if not match:
        return None
    try:
        return datetime.strptime(
            match.group(1), "%Y%m%d%H%M%S"
        ).replace(tzinfo=timezone.utc).isoformat()
    except ValueError:
        return None


def _scan_json(raw: Any) -> tuple[set[str], set[str], set[str], int, bool]:
    symbols: set[str] = set()
    fields: set[str] = set()
    capture_fields: set[str] = set()
    stack: list[tuple[Any, int]] = [(raw, 0)]
    visited = 0
    while stack and visited < MAX_SCAN_OBJECTS:
        node, depth = stack.pop()
        visited += 1
        if isinstance(node, dict):
            for key, value in node.items():
                if type(key) is not str:
                    continue
                fields.add(key)
                if key.lower() in _CAPTURE_TOKENS:
                    capture_fields.add(key)
                if SYM.fullmatch(key):
                    symbols.add(key)
                if key.lower() in ("symbol", "contract", "contractsymbol",
                                   "contract_symbol", "pair", "instid"):
                    if type(value) is str and SYM.fullmatch(value):
                        symbols.add(value)
                if depth < 8 and isinstance(value, (dict, list)):
                    stack.append((value, depth + 1))
        elif isinstance(node, list) and depth < 8:
            stack.extend((child, depth + 1) for child in node
                         if isinstance(child, (dict, list)))
    return symbols, fields, capture_fields, visited, bool(stack)


def _csv_scan(path: Path) -> tuple[set[str], set[str], set[str], int, bool]:
    symbols: set[str] = set()
    with path.open("r", encoding="utf-8-sig", newline="") as fh:
        reader = csv.DictReader(fh)
        if reader.fieldnames is None or len(set(reader.fieldnames)) != len(reader.fieldnames):
            raise ValueError("CSV_SCHEMA_INVALID")
        fields = set(reader.fieldnames)
        capture_fields = set(x for x in fields if x.lower() in _CAPTURE_TOKENS)
        count = 0
        for row in reader:
            count += 1
            for field in ("symbol", "contract_symbol", "contractSymbol", "pair"):
                s = row.get(field)
                if type(s) is str and SYM.fullmatch(s):
                    symbols.add(s)
            if count >= MAX_CSV_ROWS:
                return symbols, fields, capture_fields, count, True
        return symbols, fields, capture_fields, count, False


def inspect_source(root: Path, relative: str,
                   entry_symbols: set[str]) -> dict[str, Any]:
    result: dict[str, Any] = {
        "relative_path": relative,
        "status": "NOT_CHECKED",
        "filename_time_hint_unverified": _hint(relative),
        "historical_capture_provenance_certified": False,
        "historical_capacity_certified": False,
    }
    try:
        target = (root / relative).resolve()
        target.relative_to(root.resolve())
        if not target.is_file():
            result["status"] = "FILE_NOT_FOUND"
            return result
        size = target.stat().st_size
        result["bytes"] = size
        if size > MAX_SOURCE_BYTES:
            result["status"] = "FILE_TOO_LARGE_FOR_SAFE_INVENTORY"
            return result
        if target.suffix.lower() == ".json":
            with target.open("r", encoding="utf-8-sig") as fh:
                raw = json.load(fh)
            symbols, fields, captures, visited, truncated = _scan_json(raw)
            result["schema_kind"] = type(raw).__name__
            result["objects_examined"] = visited
        elif target.suffix.lower() == ".csv":
            symbols, fields, captures, visited, truncated = _csv_scan(target)
            result["schema_kind"] = "CSV"
            result["records_examined"] = visited
        else:
            result["status"] = "SOURCE_FORMAT_UNSUPPORTED"
            return result
        result.update({
            "status": "SCHEMA_SURVEY_TRUNCATED" if truncated else "SCHEMA_SURVEYED_NONAUTHORIZING",
            "sha256": _hash_file(target),
            "fields_sample": sorted(fields)[:_FIELDS_LIMIT],
            "capture_time_field_name_candidates": sorted(captures),
            "symbols_discovered": len(symbols),
            "distinct_entry_symbols_matched": len(symbols & entry_symbols),
            "source_symbol_extraction_incomplete": truncated,
        })
        return result
    except (OSError, ValueError, TypeError, json.JSONDecodeError, UnicodeError, csv.Error):
        result["status"] = "SOURCE_PARSE_FAILED"
        return result


def survey(root: Path, archive: Path, *,
           relative_paths: tuple[str, ...] = SOURCE_PATHS,
           require_pinned: bool = True) -> dict[str, Any]:
    if require_pinned and _hash_file(archive) != PINNED_FROZEN_ARCHIVE_SHA256:
        return {"status": "V40_ARCHIVE_SHA_MISMATCH",
                "historical_capacity_certified": False,
                "live_authorization": False}
    entries = load_entry_ledger(
        archive, capital=10000, cost_bps=100, mode="HYBRID",
    )
    if require_pinned and len(entries) != 108:
        return {"status": "V40_ENTRY_COUNT_MISMATCH",
                "historical_capacity_certified": False,
                "live_authorization": False}
    symbols = {row["symbol"] for row in entries}
    sources = [inspect_source(root, relative, symbols)
               for relative in relative_paths]
    return {
        "status": "V40_LOCAL_SOURCE_INVENTORY_NONAUTHORIZING",
        "frozen_archive_sha256": _hash_file(archive),
        "entries_in_frozen_hybrid": len(entries),
        "distinct_entry_symbols": len(symbols),
        "sources_examined": len(sources),
        "sources": sources,
        "filename_dates_and_timestamp_field_names_are_not_provenance": True,
        "point_in_time_contract_coverage_certified": False,
        "historical_execution_capacity_certified": False,
        "entry_allowed": False,
        "live_authorization": False,
        "mexc_orders_sent": False,
        "read_only": True,
    }


def main() -> None:
    p = argparse.ArgumentParser(
        description="Local-only V40 contract source structure discovery"
    )
    p.add_argument("--root", type=Path, default=Path("~/trader-backtest").expanduser())
    p.add_argument("--archive", type=Path, default=Path("~/v40_parity_for_live.zip").expanduser())
    p.add_argument("--output", type=Path)
    args = p.parse_args()
    try:
        result = survey(args.root.expanduser(), args.archive.expanduser())
    except Exception as exc:
        result = {
            "status": "V40_LOCAL_SOURCE_INVENTORY_HALTED",
            "error_type": type(exc).__name__,
            "historical_capacity_certified": False,
            "live_authorization": False,
        }
    output = json.dumps(result, indent=2, sort_keys=True)
    if args.output is not None:
        args.output.expanduser().write_text(output + "\n", encoding="utf-8")
    print(output)


if __name__ == "__main__":
    main()
