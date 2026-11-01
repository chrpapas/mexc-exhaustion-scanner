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
