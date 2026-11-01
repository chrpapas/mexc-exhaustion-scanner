"""Offline tests: injected V40 candidate filter, never execute real simulator."""
from __future__ import annotations

import hashlib
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from scripts import v40_api_filtered_original_runner as runner


class V40ReplayHookTests(unittest.TestCase):
    def setUp(self):
        self.symbols = [f"TEST{i:03d}_USDT" for i in range(111)]
        self.pool = [type("Candidate", (), {"symbol": symbol})()
                     for symbol in self.symbols]
        self.pool += self.pool[:35]  # 146 signal instances, 111 contracts
        self.allow = set(self.symbols[:98])
        self.deny = set(self.symbols[98:])

    def test_filter_drops_all_unsupported_symbols_from_candidate_pool(self):
        src = {4.0: list(self.pool)}
        baseline = runner.filter_pools(src, self.allow, self.deny,
                                       baseline=True)
        self.assertIs(baseline, src)
        filtered = runner.filter_pools(src, self.allow, self.deny)
        self.assertIsNot(src, filtered)
        self.assertEqual(len(src[4.0]), 146)
        self.assertEqual(len(filtered[4.0]), 133)
        self.assertEqual(len({x.symbol for x in filtered[4.0]}), 98)
        self.assertFalse(any(x.symbol in self.deny for x in filtered[4.0]))

    def test_extra_floor_also_filters_and_unknown_candidate_blocks(self):
        src = {4.0: list(self.pool), 5.0: tuple(self.pool[-6:])}
        r = runner.filter_pools(src, self.allow, self.deny)
        self.assertIsInstance(r[5.0], tuple)
        self.assertTrue(all(x.symbol in self.allow for x in r[5.0]))
        src[5.0] = [type("Candidate", (), {"symbol": "OTHER_USDT"})()]
        with self.assertRaisesRegex(ValueError, "UNKNOWN_CANDIDATE_IN_OTHER_FLOOR"):
            runner.filter_pools(src, self.allow, self.deny)

    def test_source_ast_wrapping_preserves_original_source(self):
        source_text = """
def main():
    pools = build_floor_pools("frozen")
    return len(pools[4.0])
"""
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "contract_replay_v40_scale.py"
            path.write_text(source_text)
            sha = hashlib.sha256(path.read_bytes()).hexdigest()
            with patch.object(runner, "PINNED_ORIGINAL_SHA", sha):
                tree, digest = runner.prepare_original(path)
            self.assertEqual(digest, sha)
            self.assertEqual(path.read_text(), source_text)
            call_counts = []
            def build_floor_pools(value):
                self.assertEqual(value, "frozen")
                return {4.0: list(self.pool)}
            def hook(pools):
                call_counts.append(len(pools[4.0]))
                return runner.filter_pools(pools, self.allow, self.deny)
            scope = {"build_floor_pools": build_floor_pools,
                     "__t100_v40_prefilter_pools__": hook}
            exec(compile(tree, str(path), "exec"), scope)
            self.assertEqual(scope["main"](), 133)
            self.assertEqual(call_counts, [146])

    def test_candidate_only_stop_prevents_portfolio_execution(self):
        # Even a valid filter should not invoke the expensive portfolio
        # replay during --probe-only diagnosis.
        source_text = """
def main():
    pool = build_floor_pools("frozen")
    portfolio_replay_should_not_execute()
    return pool
if __name__ == "__main__":
    main()
"""
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "src.py"
            path.write_text(source_text)
            sha = hashlib.sha256(path.read_bytes()).hexdigest()
            with patch.object(runner, "PINNED_ORIGINAL_SHA", sha):
                tree, _ = runner.prepare_original(path)
            calls = []
            def filter_then_stop(pools):
                result = runner.filter_pools(pools, self.allow, self.deny)
                self.assertEqual(len(result[4.0]), 133)
                raise runner.CandidateHookProbeComplete()
            def should_not_execute():
                calls.append("BAD")
            scope = {
                "__name__": "__main__",
                "build_floor_pools": lambda _: {4.0: list(self.pool)},
                "__t100_v40_prefilter_pools__": filter_then_stop,
                "portfolio_replay_should_not_execute": should_not_execute,
            }
            with self.assertRaises(runner.CandidateHookProbeComplete):
                exec(compile(tree, str(path), "exec"), scope)
            self.assertEqual(calls, [])


    def test_original_fixed_count_checks_patch_only_for_filtered_mode(self):
        # Faithful AST shapes of original simulator lines 358, 470, 485,
        # 489 and 497, with the other stage/accounting checks left intact.
        sample = """
import math
FROZEN_ENDPOINTS = {("A", 100.): 1.0, ("B", 100.): 1.0,
                    ("A", 200.): 1.0, ("B", 200.): 1.0}
INITIAL_CAPITAL = 2000.
class FakeRun:
    def metrics(self, count):
        return {"observed_candidate_count": count}
def reports(reg, runs):
    r = runs["A"]
    m = r.metrics(146)
    return m
def main():
    v39_reference_capital = 2000.
    candidates = build_floor_pools("frozen")[4.]
    if len(candidates)!=146: raise RuntimeError('CANDIDATE_COUNT_CHANGED')
    if BROKEN_ACCOUNT_CHECK: raise RuntimeError("ACCOUNT_CHECK_FAILED")
    m = reports(None, {"A": FakeRun()})
    parity = []
    for p,c in (FROZEN_ENDPOINTS if math.isclose(
        INITIAL_CAPITAL,v39_reference_capital,rel_tol=0.0,abs_tol=1e-9
    ) else {}):
        parity.append((p,c))
    if math.isclose(
        INITIAL_CAPITAL,v39_reference_capital,rel_tol=0.0,abs_tol=1e-9
    ) and len(parity)!=4:
        raise RuntimeError('INCOMPLETE_REFERENCE_PARITY')
    return len(candidates),m["observed_candidate_count"],len(parity)
"""
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "original.py"
            path.write_text(sample)
            sha = hashlib.sha256(path.read_bytes()).hexdigest()
            def run(*, filtered, baseline, broken_account=False):
                with patch.object(runner, "PINNED_ORIGINAL_SHA", sha):
                    tree, digest = runner.prepare_original(
                        path, filtered_mode=filtered,
                    )
                self.assertEqual(digest, sha)
                runtime = {"__name__": "unit_test",
                           "__t100_v40_baseline_mode__": baseline,
                           "__t100_v40_expected_candidate_count__": 146,
                           "BROKEN_ACCOUNT_CHECK": broken_account}
                def hook(pools):
                    result = runner.filter_pools(pools, self.allow, self.deny,
                                                 baseline=baseline)
                    runtime["__t100_v40_expected_candidate_count__"] = len(result[4.])
                    return result
                runtime["__t100_v40_prefilter_pools__"] = hook
                runtime["build_floor_pools"] = lambda _: {4.: list(self.pool)}
                exec(compile(tree, str(path), "exec"), runtime)
                return runtime["main"]()
            self.assertEqual(run(filtered=False, baseline=True),
                             (146, 146, 4))
            self.assertEqual(run(filtered=True, baseline=False),
                             (133, 133, 0))
            with self.assertRaisesRegex(RuntimeError, "CANDIDATE_COUNT_CHANGED"):
                run(filtered=False, baseline=False)
            with self.assertRaisesRegex(RuntimeError, "ACCOUNT_CHECK_FAILED"):
                run(filtered=True, baseline=False, broken_account=True)
            self.assertEqual(path.read_text(), sample)

    def test_adapter_fails_closed_if_pinned_original_layout_changes(self):
        sample = """
def reports(reg, runs):
    return runs["A"].metrics(146)
def main():
    candidates = build_floor_pools("frozen")[4.]
    if len(candidates)!=146: raise RuntimeError('CANDIDATE_COUNT_CHANGED')
"""
        tree = __import__("ast").parse(sample)
        with self.assertRaisesRegex(
            ValueError, "FILTERED_REPLAY_FIXED_CHECKS_UNVERIFIED"
        ):
            runner.adapt_filtered_replay_checks(tree)

    def test_wrong_source_hash_and_multiple_calls_fail(self):
        with tempfile.TemporaryDirectory() as tmp:
            source = Path(tmp) / "src.py"
            source.write_text("def main():\n    return 1\n")
            with self.assertRaisesRegex(ValueError, "ORIGINAL_SOURCE_SHA_MISMATCH"):
                runner.prepare_original(source)
            text = """
def main():
    a = build_floor_pools(1)
    return build_floor_pools(2)
"""
            source.write_text(text)
            with patch.object(runner, "PINNED_ORIGINAL_SHA",
                              hashlib.sha256(source.read_bytes()).hexdigest()):
                with self.assertRaisesRegex(ValueError, "ORIGINAL_POOL_CALL_NOT_IN_MAIN"):
                    runner.prepare_original(source)


if __name__ == "__main__":
    unittest.main()
