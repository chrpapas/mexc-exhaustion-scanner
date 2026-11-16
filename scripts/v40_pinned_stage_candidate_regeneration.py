"""Recreate frozen V40's 146 source candidate keys from its historical stage CSV.

This reproduces ORIGINAL archived stage_reference/ATR4 source code, hash pinned
to the 2026-11-02 research provenance. It is not an independent candidate
generator, a full historical portfolio replay, or MEXC execution permission.
No original/SSD data is written, no exchange/DB/network/API clients used.
"""
from __future__ import annotations

import argparse
import csv
import hashlib
import importlib.util
import io
import json
import sys
from pathlib import Path
from zipfile import ZipFile

FROZEN_ARCHIVE_SHA256 = "2918f4589e8d564ca55b6f24aa2e9a868b5d214653ad7d92acaf1729fd1e4cb5"
FROZEN_STAGE_CSV_SHA256 = "94d41f5077324673d79dc367f5b66b41732ebf1c37e4e046c9ae09bc72b795ca"
FROZEN_STAGE_MODULE_SHA256 = "7ff24d0bb4e55344698bfe9b6a27da7c2ac5c4ec7b09edce62733835b9927105ed4"
FROZEN_ATR_MODULE_SHA256 = "8d81f8f5bb24fdd5993874da747a3c397e7e97ce86994db1144178be5ecd7141"
EXPECTED_CANDIDATES = 146
FIELDS = ("symbol", "entry_ts", "entry_reference_price", "stage1_atr7_pct", "episode_id")


class FrozenCandidateSourceHalted(ValueError):
    """No sensitive local values or historical symbols in error codes."""


def sha256_file(path: Path, *, limit: int) -> str:
    if not path.is_file() or path.stat().st_size > limit or path.stat().st_size < 1:
        raise FrozenCandidateSourceHalted("PINNED_SOURCE_MISSING_OR_OVERSIZED")
    hash_ = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            hash_.update(chunk)
    return hash_.hexdigest()


def compare_candidate_rows(
    frozen: list[dict[str, str]],
    regenerated: list[dict[str, str]],
    *,
    expected_count: int = EXPECTED_CANDIDATES,
) -> int:
    """Exact ordered original-export comparison; no optimistic rounding."""
    if (
        len(frozen) != expected_count
        or len(regenerated) != expected_count
        or any(set(FIELDS) - set(r) for r in frozen)
        or any(set(FIELDS) - set(r) for r in regenerated)
    ):
        raise FrozenCandidateSourceHalted("CANDIDATE_ROW_COUNT_OR_FIELDS_INVALID")
    frozen_keys = [(r["symbol"], r["entry_ts"]) for r in frozen]
    regenerated_keys = [(r["symbol"], r["entry_ts"]) for r in regenerated]
    if len(set(frozen_keys)) != expected_count or len(set(regenerated_keys)) != expected_count:
        raise FrozenCandidateSourceHalted("CANDIDATE_IDENTITY_DUPLICATE")
    if any(
        a[field] != b[field] for a,b in zip(frozen, regenerated)
        for field in FIELDS
    ):
        raise FrozenCandidateSourceHalted("PINNED_ORIGINAL_CANDIDATE_REGENERATION_DRIFT")
    return len(frozen)


def _load_pinned_module(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:
        raise FrozenCandidateSourceHalted("PINNED_MODULE_IMPORT_INVALID")
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def run(*, stage_csv: Path, archive: Path, source_dir: Path) -> dict[str, object]:
    stage_csv = stage_csv.expanduser().resolve()
    archive = archive.expanduser().resolve()
    source_dir = source_dir.expanduser().resolve()
    stage_module = source_dir / "stage_reference.py"
    atr_module = source_dir / "atr_floor_v16.py"
    actual_hashes = {
        "stage_csv": sha256_file(stage_csv, limit=100_000_000),
        "archive": sha256_file(archive, limit=10_000_000),
        "stage_module": sha256_file(stage_module, limit=2_000_000),
        "atr_module": sha256_file(atr_module, limit=2_000_000),
    }
    expected_hashes = {
        "stage_csv": FROZEN_STAGE_CSV_SHA256,
        "archive": FROZEN_ARCHIVE_SHA256,
        "stage_module": FROZEN_STAGE_MODULE_SHA256,
        "atr_module": FROZEN_ATR_MODULE_SHA256,
    }
    if actual_hashes != expected_hashes:
        raise FrozenCandidateSourceHalted("PINNED_SOURCE_SHA256_MISMATCH")

    with ZipFile(archive) as z:
        manifest = json.loads(z.read("manifest.json"))
        if (
            manifest.get("candidate_count") != EXPECTED_CANDIDATES
            or manifest.get("stage_sha256") != FROZEN_STAGE_CSV_SHA256
            or manifest.get("type") != "READ_ONLY_SOURCE_PARITY_SAMPLE"
            or manifest.get("no_exchange_keys_or_orders") is not True
        ):
            raise FrozenCandidateSourceHalted("FROZEN_STAGE_MANIFEST_MISMATCH")
        info = z.getinfo("candidates.csv")
        if info.file_size < 1 or info.file_size > 10_000_000:
            raise FrozenCandidateSourceHalted("FROZEN_CANDIDATES_INVALID_SIZE")
        frozen = list(csv.DictReader(
            io.StringIO(z.read("candidates.csv").decode("utf-8-sig"))
        ))

    # Executing the original, hash-pinned historical research source still
    # implies trust in its transitive imports. It does NOT prove an independent
    # mathematical Stage2 detector or any exchange execution properties.
    old_path = list(sys.path)
    previous = {n:sys.modules.get(n) for n in ("stage_reference", "atr_floor_v16")}
    try:
        sys.path.insert(0, str(source_dir))
        stage = _load_pinned_module("stage_reference", stage_module)
        atr = _load_pinned_module("atr_floor_v16", atr_module)
        episodes, _, anchors = stage.load_exact_v2_stage_dataset(stage_csv)
        if stage.validate_exact_v2_dataset(anchors) is not True:
            raise FrozenCandidateSourceHalted("V2_STAGE_DATASET_ANCHORS_FAILED")
        cohort = stage.build_candidate_sets_exact_v2(episodes)["P15_A0_D0"]
        candidates = atr.build_floor_pools(cohort, floors=(4.,))[4.]
        regenerated = [
            {
                "symbol": str(c.symbol),
                "entry_ts": str(c.entry_ts),
                "entry_reference_price": str(c.entry),
                "stage1_atr7_pct": str(c.stage1_atr7_pct),
                "episode_id": str(c.episode_id),
            } for c in candidates
        ]
    except FrozenCandidateSourceHalted:
        raise
    except Exception:
        raise FrozenCandidateSourceHalted("PINNED_ORIGINAL_REGENERATION_FAILED") from None
    finally:
        sys.path[:] = old_path
        for name, old in previous.items():
            if old is None:
                sys.modules.pop(name, None)
            else:
                sys.modules[name] = old

    identical = compare_candidate_rows(
        frozen, regenerated, expected_count=EXPECTED_CANDIDATES
    )
    return {
        "status": "V40_PINNED_ORIGINAL_146_CANDIDATES_REGENERATED",
        "candidate_count": identical,
        "candidate_sequence_and_fields_identical": True,
        "stage_dataset_hash_verified": True,
        "original_stage_and_ATR4_module_hashes_verified": True,
        "original_source_code_reexecuted": True,
        "transitive_dependency_hashes_certified": False,
        "independent_stage2_algorithm_certified": False,
        "full_strategy_decision_parity_certified": False,
        "point_in_time_contract_specs_certified": False,
        "venue_execution_certified": False,
        "live_authorization": False,
        "mexc_posts": 0,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--stage", type=Path, required=True)
    parser.add_argument("--archive", type=Path, required=True)
    parser.add_argument("--source-dir", type=Path, required=True)
    a = parser.parse_args()
    try:
        result = run(stage_csv=a.stage, archive=a.archive, source_dir=a.source_dir)
    except (FrozenCandidateSourceHalted, OSError, ValueError, TypeError, KeyError,
            json.JSONDecodeError, UnicodeError, csv.Error) as exc:
        code = str(exc) if type(exc) is FrozenCandidateSourceHalted else "SOURCE_UNVERIFIED"
        result = {
            "status": "V40_PINNED_CANDIDATE_REGENERATION_HALTED",
            "failure_code": code,
            "live_authorization": False,
            "mexc_posts": 0,
        }
    print(json.dumps(result, indent=2, sort_keys=True))
    if result.get("status") != "V40_PINNED_ORIGINAL_146_CANDIDATES_REGENERATED":
        raise SystemExit(2)


if __name__ == "__main__":
    main()
