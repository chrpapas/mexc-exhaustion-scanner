"""Offline original V40 candidate coverage using pinned archive and prescribed trade ledger.

Explains missing pre-gate candidate rows from historical four-slot/duplicate
position state. This is NOT an independent strategy simulation: entries and
exits are prescribed. Never accesses exchange, credentials, SQL or network.
"""
from __future__ import annotations

import argparse
import csv
import hashlib
import io
import json
from collections import Counter, defaultdict
from datetime import datetime
from decimal import Decimal as D, InvalidOperation
from pathlib import Path
from zipfile import ZipFile

ARCHIVE_SHA256 = "2918f4589e8d564ca55b6f24aa2e9a868b5d214653ad7d92acaf1729fd1e4cb5"
SCENARIO = "V32_VOL30_SKIP_H4_S100_CAP5_V39_HYBRID"
EXPECTED_CANDIDATES = 146
EXPECTED_GATES = 132
EXPECTED_ENTRIES = 108
EXPECTED_CLOSED = 105
EXPECTED_FINAL_SLOTS = 3
TOLERANCE = D("0.001")


class CandidateCoverageHalted(ValueError):
    """Stable error codes: no symbols, trades, or credentials in reports."""


def _timestamp(value: str) -> int:
    try:
        x = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if x.tzinfo is None or x.utcoffset() is None:
            raise ValueError()
        t = int(x.timestamp())
        if x.timestamp() != t or t % 1800:
            raise ValueError()
        return t
    except (ValueError, TypeError, OverflowError):
        raise CandidateCoverageHalted("INVALID_HISTORICAL_TIMESTAMP") from None


def _amount(value: str) -> D:
    try:
        x = D(value)
        if not x.is_finite() or x <= 0:
            raise ValueError()
        return x
    except (InvalidOperation, ValueError, TypeError):
        raise CandidateCoverageHalted("INVALID_HISTORICAL_NOTIONAL") from None


def _rows(path: Path) -> list[dict[str, str]]:
    if not path.is_file() or path.stat().st_size > 20_000_000:
        raise CandidateCoverageHalted("HISTORICAL_LEDGER_MISSING_OR_OVERSIZED")
    try:
        with path.open(encoding="utf-8-sig", newline="") as f:
            return list(csv.DictReader(f))
    except (OSError, UnicodeError, csv.Error):
        raise CandidateCoverageHalted("HISTORICAL_LEDGER_UNREADABLE") from None


def _archive_rows(z: ZipFile, name: str) -> list[dict[str, str]]:
    info = z.getinfo(name)
    if info.file_size > 10_000_000 or info.file_size <= 0:
        raise CandidateCoverageHalted("FROZEN_MEMBER_SIZE_INVALID")
    return list(csv.DictReader(io.StringIO(z.read(name).decode("utf-8-sig"))))


def audit_candidate_coverage(archive: Path, folder: Path) -> dict[str, object]:
    """Audit omitted gate rows without claiming candidate generation parity.

    The frozen candidate CSV is not authenticated as the original simulator's
    priority order within same-bar collisions. An omitted candidate on such a
    bar remains ambiguous and cannot grant a pass.
    """
    archive = archive.expanduser().resolve()
    folder = folder.expanduser().resolve()
    if not archive.is_file() or archive.stat().st_size > 10_000_000:
        raise CandidateCoverageHalted("FROZEN_ARCHIVE_MISSING_OR_OVERSIZED")
    h = hashlib.sha256()
    with archive.open("rb") as f:
        for b in iter(lambda: f.read(1024*1024), b""):
            h.update(b)
    if h.hexdigest() != ARCHIVE_SHA256:
        raise CandidateCoverageHalted("FROZEN_ARCHIVE_HASH_MISMATCH")
    with ZipFile(archive) as z:
        manifest = json.loads(z.read("manifest.json"))
        if (manifest.get("candidate_count") != EXPECTED_CANDIDATES
                or manifest.get("no_exchange_keys_or_orders") is not True):
            raise CandidateCoverageHalted("FROZEN_MANIFEST_UNVERIFIED")
        c_rows = _archive_rows(z, "candidates.csv")
        g_rows = [r for r in _archive_rows(z, "capital_2000/gate_decisions.csv")
                  if r.get("scenario") == SCENARIO]
        archived_entries = [r for r in _archive_rows(z, "capital_2000/entries.csv")
                            if r.get("scenario") == SCENARIO]
    entries = [r for r in _rows(folder / "entries.csv")
               if r.get("scenario") == SCENARIO]
    exits = [r for r in _rows(folder / "exits.csv")
             if r.get("scenario") == SCENARIO]
    metrics = [r for r in _rows(folder / "metrics.csv")
               if r.get("scenario") == SCENARIO]
    if (len(c_rows) != EXPECTED_CANDIDATES
            or len(g_rows) != EXPECTED_GATES
            or len(entries) != EXPECTED_ENTRIES
            or len(archived_entries) != EXPECTED_ENTRIES
            or len(metrics) != 1
            or int(metrics[0].get("closed", -1)) != EXPECTED_CLOSED
            or int(metrics[0].get("open_positions", -1)) != EXPECTED_FINAL_SLOTS):
        raise CandidateCoverageHalted("FROZEN_REFERENCE_COUNTS_DRIFT")

    candidates: dict[tuple[str, int], int] = {}
    per_bar: dict[int, list[tuple[str, int]]] = defaultdict(list)
    for index, r in enumerate(c_rows):
        try:
            k = (r["symbol"], int(r["entry_ts"]))
        except (KeyError, ValueError, TypeError):
            raise CandidateCoverageHalted("FROZEN_CANDIDATE_KEY_INVALID") from None
        if k in candidates or k[1] % 1800:
            raise CandidateCoverageHalted("FROZEN_CANDIDATE_KEY_DUPLICATE_OR_UNALIGNED")
        candidates[k] = index
        per_bar[k[1]].append(k)

    gate: dict[tuple[str, int], str] = {}
    for r in g_rows:
        k = (r["symbol"], _timestamp(r["time"]))
        if k in gate or k not in candidates or r["action"] not in ("ADMIT", "SKIP"):
            raise CandidateCoverageHalted("FROZEN_GATE_KEY_OR_ACTION_INVALID")
        gate[k] = r["action"]
    archived_key = {(r["symbol"], _timestamp(r["time"])) for r in archived_entries}
    entry: dict[tuple[str, int], D] = {}
    for r in entries:
        k = (r["symbol"], _timestamp(r["time"]))
        if k in entry or k not in candidates or gate.get(k) != "ADMIT":
            raise CandidateCoverageHalted("ENTRY_NOT_UNIQUE_OR_UNADMITTED")
        entry[k] = _amount(r["notional"])
    if set(entry) != archived_key or set(entry) != {k for k,v in gate.items() if v == "ADMIT"}:
        raise CandidateCoverageHalted("FROZEN_GATE_ENTRY_IDENTITY_DRIFT")

    exit_bars: dict[int, list[dict[str, str]]] = defaultdict(list)
    for r in exits:
        if r.get("kind") not in ("normal_exit", "delayed_exit", "trim"):
            raise CandidateCoverageHalted("EXIT_PHASE_UNRECOGNIZED")
        key = (r["symbol"], _timestamp(r["entry_time"]))
        if key not in entry or _timestamp(r["exit_time"]) < key[1]:
            raise CandidateCoverageHalted("EXIT_ORIGIN_NOT_IN_ENTRIES")
        exit_bars[_timestamp(r["exit_time"])].append(r)

    active: dict[str, tuple[int, D]] = {}
    skip_reasons = Counter()
    ambiguous = 0
    unexpected = 0
    slot_peak = 0
    verified_gate_admits = 0
    seen_exit_rows = 0
    for t in sorted(set(per_bar) | set(exit_bars)):
        def close_one(row: dict[str, str]) -> None:
            nonlocal seen_exit_rows
            symbol = row["symbol"]
            original = _timestamp(row["entry_time"])
            if symbol not in active or active[symbol][0] != original:
                raise CandidateCoverageHalted("EXIT_NOT_IN_OPEN_PORTFOLIO")
            remaining = active[symbol][1] - _amount(row["notional"])
            if remaining < -TOLERANCE:
                raise CandidateCoverageHalted("EXIT_EXCEEDS_OWNED_SIZE")
            if remaining <= TOLERANCE:
                del active[symbol]
            else:
                active[symbol] = (original, remaining)
            seen_exit_rows += 1

        for row in exit_bars[t]:
            if row["kind"] != "normal_exit":
                close_one(row)

        same_bar_ambiguous = len(per_bar[t]) > 1 and any(k not in gate for k in per_bar[t])
        for k in per_bar[t]:
            sym, _ = k
            if k in gate:
                if gate[k] == "ADMIT":
                    if sym in active or len(active) >= 4:
                        raise CandidateCoverageHalted("ADMITTED_WITH_DUPLICATE_OR_FULL_SLOTS")
                    if k not in entry:
                        raise CandidateCoverageHalted("GATE_ADMIT_WITHOUT_ENTRY")
                    active[sym] = (t, entry[k])
                    slot_peak = max(slot_peak, len(active))
                    verified_gate_admits += 1
            else:
                if same_bar_ambiguous:
                    ambiguous += 1
                elif sym in active:
                    skip_reasons["duplicate_position"] += 1
                elif len(active) >= 4:
                    skip_reasons["four_slots_occupied"] += 1
                else:
                    unexpected += 1
        for row in exit_bars[t]:
            if row["kind"] == "normal_exit":
                close_one(row)

    unobserved_gate = len(candidates) - len(gate)
    if (verified_gate_admits != EXPECTED_ENTRIES or seen_exit_rows != len(exits)
            or slot_peak != 4 or len(active) != EXPECTED_FINAL_SLOTS):
        raise CandidateCoverageHalted("PORTFOLIO_TRACE_RECONCILIATION_FAILED")
    covered = sum(skip_reasons.values())
    success = (unobserved_gate == covered and ambiguous == 0 and unexpected == 0)
    return {
        "status": "V40_HISTORICAL_OMITTED_CANDIDATES_ACCOUNTED_FOR" if success
                  else "V40_HISTORICAL_OMITTED_CANDIDATE_COVERAGE_INCOMPLETE",
        "archive_sha256": h.hexdigest(),
        "candidate_count": len(candidates),
        "gate_decisions": len(gate),
        "gate_admits": verified_gate_admits,
        "gate_skips": sum(v == "SKIP" for v in gate.values()),
        "candidates_without_gate": unobserved_gate,
        "explained_four_slots": skip_reasons["four_slots_occupied"],
        "explained_duplicate_position": skip_reasons["duplicate_position"],
        "ambiguous_same_bar_order": ambiguous,
        "unexplained_omissions": unexpected,
        "exits_applied": seen_exit_rows,
        "peak_positions": slot_peak,
        "remaining_positions": len(active),
        "omitted_candidate_coverage_verified": success,
        "strategy_decisions_independently_replayed": False,
        "stage2_signal_discovery_independently_replayed": False,
        "entry_size_path_independently_replayed": False,
        "venue_execution_certified": False,
        "entry_allowed": False,
        "live_authorization": False,
        "mexc_posts": 0,
    }


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--archive", type=Path, required=True)
    p.add_argument("--folder", type=Path, required=True)
    args = p.parse_args()
    try:
        result = audit_candidate_coverage(args.archive, args.folder)
    except (CandidateCoverageHalted, OSError, KeyError, ValueError, TypeError, json.JSONDecodeError) as exc:
        code = str(exc) if type(exc) is CandidateCoverageHalted else "INPUT_UNVERIFIED"
        result = {"status": "V40_CANDIDATE_COVERAGE_AUDIT_HALTED", "failure_code": code,
                  "live_authorization": False, "mexc_posts": 0}
    print(json.dumps(result, indent=2, sort_keys=True))
    if result["status"] != "V40_HISTORICAL_OMITTED_CANDIDATES_ACCOUNTED_FOR":
        raise SystemExit(2)


if __name__ == "__main__":
    main()
