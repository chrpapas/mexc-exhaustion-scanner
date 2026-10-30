#!/usr/bin/env python3
"""Offline batch capacity audit of EXISTING frozen V40 exported entry records.

Usage:
  PYTHONPATH=. python scripts/v40_historical_capacity_audit.py \
    ~/v40_parity_for_live.zip --capital 10000 --cost-bps 100 --mode HYBRID

Optional --evidence-jsonl uses explicitly archived contract metadata and
signal-time reference prices. No fallback to today's contract specs: old
historical contracts may no longer be valid. A missing witness is counted
as UNKNOWN, not PASS. Optional --template-output writes a local skeleton.

This script never extracts archive contents, contacts MEXC/Render/DB,
reads credentials, changes account state or sends orders. Summary output
contains no symbols or prices; --output-details is LOCAL-ONLY and may
contain sensitive archived trading research.
"""
from __future__ import annotations

import argparse
from collections import Counter
import csv
from datetime import datetime, timezone
import hashlib
import io
import json
from pathlib import Path
from typing import Any
from zipfile import ZipFile

from app.t100_live_v40_historical_capacity import (
    assess_historical_v40_entry_capacity,
)

EXPECTED_CANDIDATES = 146
EXPECTED_PRIOR_BARS = 7154
ALLOWED_CAPITALS = (2000, 10000, 100000)
ALLOWED_COSTS = (100, 200)
ALLOWED_MODES = ("REFERENCE", "HYBRID", "KNOWN_ONLY")
MAX_ARCHIVE_MEMBER_BYTES = 30_000_000
MAX_ENTRIES = 146


def _stamp(value: str) -> int:
    if type(value) is not str:
        raise ValueError("V40_ARCHIVE_TIME_INVALID")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        raise ValueError("V40_ARCHIVE_TIME_INVALID") from None
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError("V40_ARCHIVE_TIME_INVALID")
    return int(parsed.astimezone(timezone.utc).timestamp())


def _rows(z: ZipFile, name: str) -> list[dict[str, str]]:
    if name not in z.namelist():
        raise ValueError("V40_ARCHIVE_REQUIRED_MEMBER_MISSING")
    info = z.getinfo(name)
    if info.file_size > MAX_ARCHIVE_MEMBER_BYTES:
        raise ValueError("V40_ARCHIVE_MEMBER_TOO_LARGE")
    raw = z.read(info)
    parsed = csv.DictReader(io.StringIO(raw.decode("utf-8-sig")))
    if not parsed.fieldnames or len(set(parsed.fieldnames)) != len(parsed.fieldnames):
        raise ValueError("V40_ARCHIVE_CSV_HEADER_INVALID")
    return list(parsed)


def _scenario(cost: int, mode: str) -> str:
    base = f"V32_VOL30_SKIP_H4_S{cost}_CAP5"
    return base if mode == "REFERENCE" else f"{base}_V39_{mode}"


def load_entry_ledger(archive: Path, *, capital: int,
                      cost_bps: int, mode: str) -> list[dict[str, str]]:
    if capital not in ALLOWED_CAPITALS or cost_bps not in ALLOWED_COSTS or mode not in ALLOWED_MODES:
        raise ValueError("V40_ARCHIVE_SCENARIO_UNSUPPORTED")
    with ZipFile(archive) as z:
        raw_manifest = z.read("manifest.json")
        if len(raw_manifest) > 100_000:
            raise ValueError("V40_ARCHIVE_MANIFEST_TOO_LARGE")
        manifest = json.loads(raw_manifest)
        if not isinstance(manifest, dict) or any((
            manifest.get("type") != "READ_ONLY_SOURCE_PARITY_SAMPLE",
            manifest.get("candidate_count") != EXPECTED_CANDIDATES,
            manifest.get("preceding_candles_exported") != EXPECTED_PRIOR_BARS,
        )):
            raise ValueError("V40_ARCHIVE_MANIFEST_NOT_FROZEN")
        candidates = _rows(z, "candidates.csv")
        if len(candidates) != EXPECTED_CANDIDATES:
            raise ValueError("V40_ARCHIVE_CANDIDATE_COUNT_INVALID")
        candidate_keys = set()
        for row in candidates:
            symbol = row["symbol"]
            stamp = int(row["entry_ts"])
            key = (symbol, stamp)
            if key in candidate_keys:
                raise ValueError("V40_ARCHIVE_DUPLICATE_CANDIDATE")
            candidate_keys.add(key)
        prefix = f"capital_{capital}/"
        gates = _rows(z, prefix + "gate_decisions.csv")
        entries = _rows(z, prefix + "entries.csv")
        scenario = _scenario(cost_bps, mode)
        admit_keys = set()
        for row in gates:
            if row["scenario"] != scenario:
                continue
            key = (row["symbol"], _stamp(row["time"]))
            if key not in candidate_keys or key in admit_keys:
                raise ValueError("V40_ARCHIVE_GATE_IDENTITY_INVALID")
            if row["action"] == "ADMIT":
                admit_keys.add(key)
            elif row["action"] != "SKIP":
                raise ValueError("V40_ARCHIVE_GATE_ACTION_INVALID")
        selected = []
        keys = set()
        for row in entries:
            if row["scenario"] != scenario:
                continue
            key = (row["symbol"], _stamp(row["time"]))
            if key not in admit_keys or key in keys:
                raise ValueError("V40_ARCHIVE_ENTRY_IDENTITY_INVALID")
            keys.add(key)
            selected.append(row)
            if len(selected) > MAX_ENTRIES:
                raise ValueError("V40_ARCHIVE_TOO_MANY_ENTRIES")
        return sorted(selected, key=lambda row: (_stamp(row["time"]), row["symbol"]))


def read_evidence(path: Path | None) -> dict[tuple[str, int], dict[str, Any]]:
    if path is None:
        return {}
    rows: dict[tuple[str, int], dict[str, Any]] = {}
    if path.stat().st_size > 15_000_000:
        raise ValueError("V40_EVIDENCE_FILE_TOO_LARGE")
    with path.open("r", encoding="utf-8") as fh:
        for line in fh:
            if not line.strip():
                continue
            item = json.loads(line)
            if type(item) is not dict or type(item.get("symbol")) is not str:
                raise ValueError("V40_EVIDENCE_INVALID_RECORD")
            key = (item["symbol"], _stamp(item["time"]))
            if key in rows or len(rows) >= MAX_ENTRIES:
                raise ValueError("V40_EVIDENCE_DUPLICATE_OR_TOO_MANY")
            rows[key] = item
    return rows


def audit(archive: Path, *, capital: int = 10000, cost_bps: int = 100,
          mode: str = "HYBRID", evidence_jsonl: Path | None = None,
          output_details: Path | None = None,
          template_output: Path | None = None) -> dict[str, Any]:
    entries = load_entry_ledger(
        archive, capital=capital, cost_bps=cost_bps, mode=mode,
    )
    evidence = read_evidence(evidence_jsonl)
    keys = {(row["symbol"], _stamp(row["time"])) for row in entries}
    if any(key not in keys for key in evidence):
        raise ValueError("V40_EVIDENCE_HAS_NONENTRY_RECORDS")
    details = []
    statuses = Counter()
    for row in entries:
        key = (row["symbol"], _stamp(row["time"]))
        outcome = assess_historical_v40_entry_capacity(row, evidence.get(key))
        statuses[outcome.status] += 1
        details.append({
            "symbol": row["symbol"], "time": row["time"],
            "status": outcome.status,
            "pre_lot_reference_notional_usdt": str(
                outcome.pre_lot_reference_notional_usdt
            ) if outcome.pre_lot_reference_notional_usdt is not None else None,
            "exported_entry_notional_usdt": str(
                outcome.exported_entry_notional_usdt
            ) if outcome.exported_entry_notional_usdt is not None else None,
            "public_position_tier_fits": outcome.public_position_tier_fits,
            "public_single_order_fits": outcome.public_single_order_fits,
            "point_in_time_evidence_supplied": outcome.point_in_time_evidence_supplied,
            "historical_execution_capacity_certified": False,
            "live_authorization": False,
        })
    if output_details is not None:
        output_details.write_text(json.dumps({
            "mode": mode, "capital": capital, "cost_bps": cost_bps,
            "entries": details, "research_only": True,
        }, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    if template_output is not None:
        with template_output.open("w", encoding="utf-8") as out:
            for row in entries:
                out.write(json.dumps({
                    "symbol": row["symbol"], "time": row["time"],
                    "entry_reference_price": None,
                    "price_observed_at": None,
                    "contract_observed_at": None,
                    "contract": None,
                }, sort_keys=True) + "\n")
    sha = hashlib.sha256()
    with archive.open("rb") as source:
        for chunk in iter(lambda: source.read(1048576), b""):
            sha.update(chunk)
    return {
        "status": (
            "V40_HISTORICAL_CAPACITY_PUBLIC_BATCH_OBSERVED_NONAUTHORIZING"
            if entries and sum(statuses[s] for s in (
                "V40_HISTORICAL_PUBLIC_CAP_FITS_NONAUTHORIZING",
                "V40_HISTORICAL_PUBLIC_POSITION_CAP_REJECT",
                "V40_HISTORICAL_PUBLIC_SINGLE_ORDER_CAP_REJECT",
                "V40_HISTORICAL_PUBLIC_MINIMUM_ORDER_NOT_MET",
            )) == len(entries)
            else "V40_HISTORICAL_CAPACITY_EVIDENCE_INCOMPLETE"
        ),
        "archive_sha256": sha.hexdigest(),
        "capital": capital, "cost_bps": cost_bps, "mode": mode,
        "entries_checked": len(entries),
        "outcome_counts": dict(sorted(statuses.items())),
        "public_point_in_time_data_independently_authenticated": False,
        "private_margin_or_liquidity_certified": False,
        "historical_execution_capacity_certified": False,
        "entry_allowed": False, "live_authorization": False,
        "mexc_orders_sent": False, "read_only": True,
    }


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("archive", type=Path)
    p.add_argument("--capital", type=int, choices=ALLOWED_CAPITALS, default=10000)
    p.add_argument("--cost-bps", type=int, choices=ALLOWED_COSTS, default=100)
    p.add_argument("--mode", choices=ALLOWED_MODES, default="HYBRID")
    p.add_argument("--evidence-jsonl", type=Path)
    p.add_argument("--output-details", type=Path)
    p.add_argument("--template-output", type=Path)
    options = p.parse_args()
    try:
        result = audit(
            options.archive, capital=options.capital,
            cost_bps=options.cost_bps, mode=options.mode,
            evidence_jsonl=options.evidence_jsonl,
            output_details=options.output_details,
            template_output=options.template_output,
        )
    except Exception as exc:
        # Operator output must not contain user path or raw archived values.
        result = {
            "status": "V40_HISTORICAL_CAPACITY_BATCH_HALTED",
            "failure_type": type(exc).__name__,
            "live_authorization": False,
            "mexc_orders_sent": False,
        }
    print(json.dumps(result, sort_keys=True, indent=2))


if __name__ == "__main__":
    main()
