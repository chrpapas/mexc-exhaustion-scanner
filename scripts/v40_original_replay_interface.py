"""Inspect a local V40 original replay source WITHOUT executing or editing it.

Identifies CLI flags and candidate admission injection points for a safe,
separately-directed filtered chronological replay. Never connects to MEXC,
reads secrets, outputs historical contract symbols, or modifies source files.
"""
from __future__ import annotations

import argparse
import ast
import hashlib
import json
from pathlib import Path


def inspect_v40_source(path: Path) -> dict[str, object]:
    if not path.is_file() or path.stat().st_size > 2_000_000:
        raise ValueError("RUNNER_NOT_FOUND_OR_TOO_LARGE")
    raw = path.read_bytes()
    tree = ast.parse(raw.decode("utf-8-sig"), filename="<local_v40_source>")
    imports = set()
    defs = set()
    arguments = set()
    calls = {}
    out_paths = []
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module:
            for item in node.names:
                imports.add(f"{node.module}:{item.name}")
        elif isinstance(node, ast.Import):
            imports.update(x.name for x in node.names)
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            defs.add(node.name)
        elif isinstance(node, ast.Call):
            func = node.func
            name = (func.id if isinstance(func, ast.Name)
                    else func.attr if isinstance(func, ast.Attribute)
                    else "OTHER")
            calls[name] = calls.get(name, 0) + 1
            if name == "add_argument":
                for arg in node.args:
                    if (isinstance(arg, ast.Constant)
                            and type(arg.value) is str
                            and arg.value.startswith("--")):
                        arguments.add(arg.value)
        elif isinstance(node, ast.Constant) and type(node.value) is str:
            if "output" in node.value.lower() and len(node.value) <= 100:
                out_paths.append(node.value)
    # Never expose arbitrary strings/paths from the source program.
    safe_flags = sorted(a for a in arguments
                        if all(c.islower() or c.isdigit() or c == "-" for c in a[2:]))
    return {
        "status": "LOCAL_V40_ORIGINAL_SOURCE_STATIC_INSPECTION",
        "source_basename": path.name,
        "source_sha256": hashlib.sha256(raw).hexdigest(),
        "has_main": "main" in defs,
        "has_chronological_replay_definition": "chronological_replay" in defs,
        "calls_chronological_replay": calls.get("chronological_replay", 0),
        "calls_build_floor_pools": calls.get("build_floor_pools", 0),
        "imports_candidate_pool_function": any(
            v.endswith(":build_floor_pools") or v == "atr_floor_v16"
            for v in imports
        ),
        "has_initial_capital_flag": "--initial-capital" in arguments,
        "cli_flags": safe_flags,
        "output_related_source_literals_count": len(out_paths),
        "source_executed": False,
        "portfolio_replay_performed": False,
        "live_authorization": False,
        "mexc_orders_sent": False,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("source", type=Path)
    args = parser.parse_args()
    try:
        report = inspect_v40_source(args.source.expanduser())
    except Exception:
        report = {
            "status": "LOCAL_V40_SOURCE_INSPECTION_UNVERIFIED",
            "source_executed": False, "portfolio_replay_performed": False,
            "live_authorization": False, "mexc_orders_sent": False,
        }
    print(json.dumps(report, indent=2, sort_keys=True))
    if report["status"] != "LOCAL_V40_ORIGINAL_SOURCE_STATIC_INSPECTION":
        raise SystemExit(2)


if __name__ == "__main__":
    main()
