"""Fail-closed AST hook for the original local V40 chronological simulator.

Wraps its single build_floor_pools(...) call to filter the 146 *candidate*
signals BEFORE replay, without altering the original source or frozen data.
Only the explicitly observed source SHA is accepted. Default is DRY RUN.
The historical simulator is called only with --execute; no MEXC/network API
or trading adapter is imported by this wrapper. Original simulator execution
is an OFFLINE research operation and never an authorization to trade.
"""
from __future__ import annotations

import argparse
import ast
import csv
import hashlib
import io
import json
import os
from pathlib import Path
import re
import sys
import traceback
from typing import Any
from zipfile import ZipFile

from app.t100_live_cross_risk_diagnostic import (
    CrossRiskDiagnosticHalted, require_readonly_controls,
)

PINNED_ORIGINAL_SHA = "bf8ba6a08332abdb5bdddd38356249a08b8c3e85d4d695a426bbd51b3d51867c"
PINNED_ARCHIVE_SHA = "2918f4589e8d564ca55b6f24aa2e9a868b5d214653ad7d92acaf1729fd1e4cb5"
SYM = re.compile(r"[A-Z0-9]+_USDT\Z")


def file_hash(path: Path) -> str:
    d = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1024 * 1024), b""):
            d.update(chunk)
    return d.hexdigest()


def validate_manifest(path: Path, archive: Path) -> tuple[set[str], set[str]]:
    if file_hash(archive) != PINNED_ARCHIVE_SHA:
        raise ValueError("FROZEN_ARCHIVE_SHA_MISMATCH")
    data = json.loads(path.read_text(encoding="utf-8"))
    if (type(data) is not dict
            or data.get("type") != "V40_CURRENT_PUBLIC_API_CANDIDATE_PREFILTER"
            or data.get("archive_sha256") != PINNED_ARCHIVE_SHA
            or data.get("candidate_count") != 146
            or data.get("candidate_unique_contracts") != 111
            or data.get("scenario") != "V32_VOL30_SKIP_H4_S100_CAP5_V39_HYBRID"):
        raise ValueError("FILTER_MANIFEST_NOT_VERIFIED")
    allow = data.get("candidate_symbols_current_public_eligible")
    deny = data.get("candidate_symbols_current_public_ineligible")
    if (type(allow) is not list or type(deny) is not list
            or any(type(s) is not str or SYM.fullmatch(s) is None
                   for s in allow + deny)
            or len(set(allow)) != len(allow) or len(set(deny)) != len(deny)
            or set(allow) & set(deny) or len(allow) + len(deny) != 111
            or len(allow) != 98 or len(deny) != 13):
        raise ValueError("FILTER_COHORT_NOT_VERIFIED")
    with ZipFile(archive) as z:
        if z.getinfo("candidates.csv").file_size > 2_000_000:
            raise ValueError("CANDIDATE_SOURCE_TOO_LARGE")
        rows = list(csv.DictReader(
            io.StringIO(z.read("candidates.csv").decode("utf-8-sig"))
        ))
    source_symbols = {r.get("symbol") for r in rows}
    if (len(rows) != 146 or len(source_symbols) != 111
            or source_symbols != set(allow) | set(deny)):
        raise ValueError("FILTER_COHORT_ARCHIVE_MISMATCH")
    return set(allow), set(deny)


class _WrapPoolCall(ast.NodeTransformer):
    def __init__(self):
        self.calls = 0

    def visit_Call(self, node: ast.Call) -> Any:
        self.generic_visit(node)
        if (isinstance(node.func, ast.Name)
                and node.func.id == "build_floor_pools"):
            self.calls += 1
            return ast.copy_location(
                ast.Call(
                    func=ast.Name(id="__t100_v40_prefilter_pools__", ctx=ast.Load()),
                    args=[node], keywords=[],
                ), node,
            )
        return node



def adapt_filtered_replay_checks(tree: ast.Module) -> tuple[ast.Module, dict[str, int]]:
    """Only remove original-146 assumptions in the pinned, filtered replay.

    All unrelated dataset, capacity, cashflow, exit and funding checks are
    untouched. Baseline mode never calls this adapter. Every requested
    in-memory AST change must match exactly one known source construct.
    """
    main_nodes = [
        n for n in tree.body
        if isinstance(n, ast.FunctionDef) and n.name == "main"
    ]
    reports_nodes = [
        n for n in tree.body
        if isinstance(n, ast.FunctionDef) and n.name == "reports"
    ]
    if len(main_nodes) != 1 or len(reports_nodes) != 1:
        raise ValueError("FILTERED_REPLAY_SOURCE_LAYOUT_CHANGED")
    main_node, reports_node = main_nodes[0], reports_nodes[0]
    counters = {
        "candidate_guard": 0,
        "metric_count": 0,
        "endpoint_loop": 0,
        "endpoint_count_guard": 0,
    }

    def raises(node: ast.If, code: str) -> bool:
        if len(node.body) != 1 or not isinstance(node.body[0], ast.Raise):
            return False
        exc = node.body[0].exc
        return (
            isinstance(exc, ast.Call)
            and isinstance(exc.func, ast.Name)
            and exc.func.id == "RuntimeError"
            and len(exc.args) == 1
            and isinstance(exc.args[0], ast.Constant)
            and exc.args[0].value == code
        )

    for node in ast.walk(main_node):
        if isinstance(node, ast.If) and raises(node, "CANDIDATE_COUNT_CHANGED"):
            if ast.unparse(node.test) != "len(candidates) != 146":
                raise ValueError("FILTERED_REPLAY_CANDIDATE_GUARD_UNEXPECTED")
            node.test = ast.parse(
                "len(candidates) != __t100_v40_expected_candidate_count__",
                mode="eval",
            ).body
            counters["candidate_guard"] += 1
        if isinstance(node, ast.For) and isinstance(node.iter, ast.IfExp):
            conditional = node.iter
            if isinstance(conditional.body, ast.Name) and conditional.body.id == "FROZEN_ENDPOINTS":
                if not (
                    isinstance(conditional.orelse, ast.Dict)
                    and not conditional.orelse.keys
                    and "math.isclose(" in ast.unparse(conditional.test)
                ):
                    raise ValueError("FILTERED_REPLAY_ENDPOINT_LOOP_UNEXPECTED")
                conditional.test = ast.BoolOp(
                    op=ast.And(), values=[
                        ast.Name(id="__t100_v40_baseline_mode__", ctx=ast.Load()),
                        conditional.test,
                    ]
                )
                counters["endpoint_loop"] += 1
        if isinstance(node, ast.If) and raises(node, "INCOMPLETE_REFERENCE_PARITY"):
            if (
                "math.isclose(" not in ast.unparse(node.test)
                or "len(parity) != 4" not in ast.unparse(node.test)
            ):
                raise ValueError("FILTERED_REPLAY_PARITY_GUARD_UNEXPECTED")
            node.test = ast.BoolOp(
                op=ast.And(), values=[
                    ast.Name(id="__t100_v40_baseline_mode__", ctx=ast.Load()),
                    node.test,
                ]
            )
            counters["endpoint_count_guard"] += 1

    for node in ast.walk(reports_node):
        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr == "metrics"
            and isinstance(node.func.value, ast.Name)
            and node.func.value.id == "r"
            and len(node.args) == 1
            and isinstance(node.args[0], ast.Constant)
            and type(node.args[0].value) is int
            and node.args[0].value == 146
            and not node.keywords
        ):
            node.args[0] = ast.Name(
                id="__t100_v40_expected_candidate_count__", ctx=ast.Load()
            )
            counters["metric_count"] += 1

    if counters != {
        "candidate_guard": 1, "metric_count": 1,
        "endpoint_loop": 1, "endpoint_count_guard": 1,
    }:
        raise ValueError("FILTERED_REPLAY_FIXED_CHECKS_UNVERIFIED")
    ast.fix_missing_locations(tree)
    return tree, counters


def prepare_original(source: Path, *, filtered_mode: bool = False) -> tuple[ast.Module, str]:
    if file_hash(source) != PINNED_ORIGINAL_SHA:
        raise ValueError("ORIGINAL_SOURCE_SHA_MISMATCH")
    if source.stat().st_size > 2_000_000:
        raise ValueError("ORIGINAL_SOURCE_TOO_LARGE")
    tree = ast.parse(source.read_text(encoding="utf-8-sig"), filename=str(source))
    mains = [n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == "main"]
    if len(mains) != 1:
        raise ValueError("ORIGINAL_MAIN_NOT_UNIQUE")
    # Ensure the hook is in the CLI main call, not in a helper imported elsewhere.
    calls_in_main = [
        node for node in ast.walk(mains[0])
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
        and node.func.id == "build_floor_pools"
    ]
    if len(calls_in_main) != 1:
        raise ValueError("ORIGINAL_POOL_CALL_NOT_IN_MAIN")
    patch = _WrapPoolCall()
    tree = patch.visit(tree)
    if patch.calls != 1:
        raise ValueError("ORIGINAL_POOL_CALL_NOT_UNIQUE")
    if filtered_mode:
        tree, _ = adapt_filtered_replay_checks(tree)
    ast.fix_missing_locations(tree)
    return tree, file_hash(source)


def filter_pools(pools: Any, allow: set[str], deny: set[str],
                 *, baseline: bool = False) -> Any:
    if not isinstance(pools, dict) or 4.0 not in pools:
        raise ValueError("ORIGINAL_CANDIDATE_POOLS_UNEXPECTED")
    base = pools[4.0]
    if not isinstance(base, (tuple, list)) or len(base) != 146:
        raise ValueError("ORIGINAL_CANDIDATE_COUNT_NOT_146")
    symbols = [getattr(x, "symbol", None) for x in base]
    if (any(type(s) is not str for s in symbols)
            or len(set(symbols)) != 111
            or set(symbols) != allow | deny):
        raise ValueError("ORIGINAL_CANDIDATES_NOT_FROZEN")
    if baseline:
        return pools
    # Filter every pool, not only 4.0, if additional floor cohorts are present.
    # This prevents unsupported symbols entering via another selectable floor.
    result = {}
    for key, candidates in pools.items():
        if not isinstance(candidates, (tuple, list)):
            raise ValueError("ORIGINAL_CANDIDATE_POOL_TYPE_UNEXPECTED")
        if any(getattr(x, "symbol", None) not in allow | deny for x in candidates):
            raise ValueError("UNKNOWN_CANDIDATE_IN_OTHER_FLOOR")
        filtered = [x for x in candidates if x.symbol in allow]
        result[key] = tuple(filtered) if isinstance(candidates, tuple) else filtered
    if len(result[4.0]) >= len(base) or len(result[4.0]) == 0:
        raise ValueError("FILTER_NOT_APPLIED")
    return result


class CandidateHookProbeComplete(BaseException):
    """Expected stop immediately after verifying the candidate filter."""


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--source", type=Path, required=True)
    p.add_argument("--archive", type=Path, required=True)
    p.add_argument("--manifest", type=Path, required=True)
    p.add_argument("--root", type=Path, required=True)
    p.add_argument("--output", type=Path, required=True)
    p.add_argument("--baseline", action="store_true")
    p.add_argument("--execute", action="store_true")
    p.add_argument("--probe-only", action="store_true",
                   help="Stop just after candidate filter, before portfolio replay")
    a = p.parse_args()
    if a.probe_only and a.execute:
        p.error("--probe-only cannot be combined with --execute")
    try:
        require_readonly_controls(os.environ)
    except CrossRiskDiagnosticHalted:
        raise SystemExit("PAPER_KILL_REQUIRED") from None
    try:
        original = a.source.expanduser().resolve()
        archive = a.archive.expanduser().resolve()
        manifest = a.manifest.expanduser().resolve()
        root = a.root.expanduser().resolve()
        output = a.output.expanduser().resolve()
        allowed, denied = validate_manifest(manifest, archive)
        tree, sha = prepare_original(original, filtered_mode=(
            not a.baseline and a.execute
        ))
        if not root.is_dir() or not output.parent.is_dir():
            raise ValueError("REPLAY_ROOT_OR_OUTPUT_PARENT_MISSING")
        if output.exists():
            raise ValueError("REPLAY_OUTPUT_ALREADY_EXISTS")
        # An output directory inside the research root is okay, but source
        # files and baseline exports must never be overwritten.
        if output == original or output == archive or output == manifest:
            raise ValueError("OUTPUT_COLLIDES_WITH_SOURCE")
        preflight = {
            "status": "V40_FILTERED_SOURCE_HOOK_PREPARED",
            "original_source_sha256": sha,
            "original_build_floor_pools_calls_wrapped": 1,
            "candidate_symbols_allowed": len(allowed),
            "candidate_symbols_excluded": len(denied),
            "baseline_mode": a.baseline,
            "execute_requested": a.execute,
            "probe_only": a.probe_only,
            "replay_performed": False,
            "live_authorization": False,
            "mexc_orders_sent": False,
        }
        print(json.dumps(preflight, indent=2, sort_keys=True), flush=True)
        if not a.execute and not a.probe_only:
            print("DRY RUN ONLY. ORIGINAL RESEARCH SOURCE AND DATA UNCHANGED.")
            return

        # An explicit opt-in triggers the archived local research engine,
        # isolated by a new, unused output path.
        # It does not import the MEXC trade adapter.
        hook_observations: list[tuple[int, int]] = []
        hook_invocations: list[int] = []
        def hook(pools: Any) -> Any:
            hook_invocations.append(1)
            if len(hook_observations) >= 1:
                raise ValueError("ORIGINAL_POOL_CALL_RUNTIME_DUPLICATED")
            transformed = filter_pools(
                pools, allowed, denied, baseline=a.baseline,
            )
            count = len(transformed[4.0])
            if (not a.baseline and count != 128) or (a.baseline and count != 146):
                raise ValueError("REPLAY_CANDIDATE_COUNT_NOT_EXPECTED")
            hook_observations.append((len(pools[4.0]), count))
            scope["__t100_v40_expected_candidate_count__"] = count
            if a.probe_only:
                raise CandidateHookProbeComplete()
            return transformed

        compiled = compile(tree, str(original), "exec")
        before_argv, before_path = sys.argv[:], sys.path[:]
        # Original source CLI accepts --root, --output and --initial-capital.
        sys.argv = [str(original), "--root", str(root),
                    "--output", str(output), "--initial-capital", "2000"]
        sys.path.insert(0, str(original.parent))
        scope = {
            "__name__": "__main__", "__file__": str(original),
            "__t100_v40_prefilter_pools__": hook,
            "__t100_v40_expected_candidate_count__": 146,
            "__t100_v40_baseline_mode__": bool(a.baseline),
        }
        try:
            try:
                exec(compiled, scope, scope)
            except CandidateHookProbeComplete:
                if not a.probe_only or len(hook_observations) != 1:
                    raise ValueError("CANDIDATE_HOOK_PROBE_UNVERIFIED")
                print(json.dumps({
                    "status": "V40_FILTERED_CANDIDATE_HOOK_PROBE_PASS",
                    "candidate_count_before": hook_observations[0][0],
                    "candidate_count_after": hook_observations[0][1],
                    "baseline_mode": a.baseline,
                    "portfolio_replay_performed": False,
                    "live_authorization": False, "mexc_orders_sent": False,
                }, indent=2, sort_keys=True))
                return
        finally:
            sys.argv = before_argv
            sys.path[:] = before_path
        if len(hook_observations) != 1:
            raise ValueError("ORIGINAL_POOL_CALL_RUNTIME_NOT_OBSERVED")
        print(json.dumps({
            "status": "LOCAL_REPLAY_PROCESS_RETURNED_REVIEW_OUTPUTS",
            "baseline_candidate_count": hook_observations[0][0],
            "input_candidate_count_after_filter": hook_observations[0][1],
            "in_memory_fixed_count_adapter": not a.baseline,
            "baseline_mode": a.baseline,
            "output_basename": output.name,
            "portfolio_metric_parity_verified": False,
            "live_authorization": False, "mexc_orders_sent": False,
        }, indent=2, sort_keys=True))
    except Exception as exc:
        safe = {
            "FROZEN_ARCHIVE_SHA_MISMATCH", "FILTER_MANIFEST_NOT_VERIFIED",
            "FILTER_COHORT_NOT_VERIFIED", "CANDIDATE_SOURCE_TOO_LARGE",
            "FILTER_COHORT_ARCHIVE_MISMATCH", "ORIGINAL_SOURCE_SHA_MISMATCH",
            "ORIGINAL_SOURCE_TOO_LARGE", "ORIGINAL_MAIN_NOT_UNIQUE",
            "ORIGINAL_POOL_CALL_NOT_IN_MAIN", "ORIGINAL_POOL_CALL_NOT_UNIQUE",
            "REPLAY_ROOT_OR_OUTPUT_PARENT_MISSING", "REPLAY_OUTPUT_ALREADY_EXISTS",
            "OUTPUT_COLLIDES_WITH_SOURCE", "ORIGINAL_CANDIDATE_POOLS_UNEXPECTED",
            "ORIGINAL_CANDIDATE_COUNT_NOT_146", "ORIGINAL_CANDIDATES_NOT_FROZEN",
            "ORIGINAL_CANDIDATE_POOL_TYPE_UNEXPECTED", "UNKNOWN_CANDIDATE_IN_OTHER_FLOOR",
            "FILTER_NOT_APPLIED", "ORIGINAL_POOL_CALL_RUNTIME_DUPLICATED",
            "FILTERED_REPLAY_SOURCE_LAYOUT_CHANGED",
            "FILTERED_REPLAY_CANDIDATE_GUARD_UNEXPECTED",
            "FILTERED_REPLAY_ENDPOINT_LOOP_UNEXPECTED",
            "FILTERED_REPLAY_PARITY_GUARD_UNEXPECTED",
            "FILTERED_REPLAY_FIXED_CHECKS_UNVERIFIED",
            "REPLAY_CANDIDATE_COUNT_NOT_EXPECTED",
            "ORIGINAL_POOL_CALL_RUNTIME_NOT_OBSERVED",
        }
        code = str(exc) if str(exc) in safe else "REPLAY_PREPARATION_OR_LOCAL_RUNTIME_ERROR"
        frames = traceback.extract_tb(exc.__traceback__)
        # Report no exception strings, paths, symbols or raw source snippets:
        # only the type and locations in verified source / wrapper code.
        original_path = locals().get("original")
        source_frame = next(
            (f for f in reversed(frames)
             if original_path is not None and Path(f.filename) == original_path),
            None,
        )
        wrapper_frame = next(
            (f for f in reversed(frames)
             if Path(f.filename).name == "v40_api_filtered_original_runner.py"),
            None,
        )
        print(json.dumps({
            "status": "V40_FILTERED_RUNNER_HALTED",
            "failure_code": code,
            "exception_class": type(exc).__name__
                if type(exc).__name__ in {
                    "RuntimeError", "ValueError", "AssertionError",
                    "KeyError", "TypeError", "IndexError", "AttributeError",
                    "FileNotFoundError", "PermissionError",
                } else "OTHER_EXCEPTION",
            "original_source_lineno": source_frame.lineno if source_frame else None,
            "original_source_function": (
                source_frame.name if source_frame
                and source_frame.name in {
                    "main", "chronological_replay", "<module>"
                } else "OTHER_OR_NOT_REACHED"
            ),
            "wrapper_lineno": wrapper_frame.lineno if wrapper_frame else None,
            "candidate_filter_hook_invocations": len(
                locals().get("hook_invocations", [])
            ),
            "candidate_filter_hook_completions": len(
                locals().get("hook_observations", [])
            ),
            "live_authorization": False, "mexc_orders_sent": False,
        }, indent=2, sort_keys=True))
        raise SystemExit(2) from None


if __name__ == "__main__":
    main()
