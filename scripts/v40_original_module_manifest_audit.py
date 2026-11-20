"""Read-only inventory of frozen V40 original research module SHA-256 identities.

Verifies all archived Python files named in Nov 2 provenance against the
operator's local ORIGINAL source directory. Never imports/executes modules,
reads credentials, mutates source or contacts an exchange. A passing manifest
cannot prove Python's whole transitive dependency graph is hash-pinned.
"""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path

EXPECTED_MODULES = 20

class ModuleManifestHalted(ValueError):
    pass


def _sha256_file(path: Path) -> str:
    if (not path.is_file() or path.is_symlink() or
        path.stat().st_size <= 0 or path.stat().st_size > 2_000_000):
        raise ModuleManifestHalted("LOCAL_MODULE_MISSING_OR_OVERSIZED")
    result = hashlib.sha256()
    with path.open("rb") as inp:
        for chunk in iter(lambda:inp.read(1<<20),b""):
            result.update(chunk)
    return result.hexdigest()


def audit(source_dir: Path, provenance: Path) -> dict[str, object]:
    source_dir = source_dir.expanduser().resolve()
    provenance = provenance.expanduser().resolve()
    if (not provenance.is_file() or provenance.is_symlink() or
        provenance.stat().st_size > 100_000):
        raise ModuleManifestHalted("PROVENANCE_FILE_MISSING_OR_OVERSIZED")
    try:
        body = json.loads(provenance.read_text(encoding="utf-8"))
        pinned = body["original_module_hashes"]
    except (OSError, ValueError, TypeError, KeyError):
        raise ModuleManifestHalted("PROVENANCE_SCHEMA_INVALID") from None
    if (not isinstance(pinned,dict) or len(pinned) != EXPECTED_MODULES or
        body.get("frozen_archive_sha256") !=
        "2918f4589e8d564ca55b6f24aa2e9a868b5d214653ad7d92acaf1729fd1e4cb5" or
        any(not isinstance(name,str) or
            not name.endswith(".py") or
            name != Path(name).name or
            not isinstance(sha,str) or len(sha)!=64 or
            any(ch not in "0123456789abcdef" for ch in sha)
            for name,sha in pinned.items())):
        raise ModuleManifestHalted("PROVENANCE_PIN_SET_INVALID")
    good = []
    mismatched = []
    absent = []
    for name, sha in sorted(pinned.items()):
        path = source_dir / name
        if not path.is_file() or path.is_symlink():
            absent.append(name)
            continue
        try:
            actual = _sha256_file(path)
        except ModuleManifestHalted:
            mismatched.append(name)
            continue
        (good if actual == sha else mismatched).append(name)
    all_ok = len(good)==EXPECTED_MODULES
    return {
        "status":("V40_ORIGINAL_SOURCE_MODULE_MANIFEST_MATCH"
                  if all_ok else "V40_ORIGINAL_SOURCE_MODULE_MANIFEST_INCOMPLETE"),
        "expected_module_count":EXPECTED_MODULES,
        "verified_module_count":len(good),
        "missing_module_count":len(absent),
        "mismatched_module_count":len(mismatched),
        "missing_module_names":absent,
        "mismatched_module_names":mismatched,
        "original_module_manifest_complete":all_ok,
        "transitive_runtime_dependency_closure_certified":False,
        "candidate_regeneration_independent_algorithm_certified":False,
        "live_authorization":False,
        "mexc_posts":0,
    }


def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument("--source-dir",required=True,type=Path)
    p.add_argument("--provenance",required=True,type=Path)
    args=p.parse_args()
    try:
        report=audit(args.source_dir,args.provenance)
    except (ModuleManifestHalted,OSError,ValueError,TypeError) as exc:
        report={"status":"V40_ORIGINAL_SOURCE_MODULE_MANIFEST_HALTED",
                "failure_code":str(exc) if type(exc) is ModuleManifestHalted else "SOURCE_UNVERIFIED",
                "live_authorization":False,"mexc_posts":0}
    print(json.dumps(report,indent=2,sort_keys=True))
    if report["status"]!="V40_ORIGINAL_SOURCE_MODULE_MANIFEST_MATCH":
        raise SystemExit(2)


if __name__=="__main__":
    main()
