"""Static-only original V40 simulator interface discovery tests."""
from __future__ import annotations

from pathlib import Path
import tempfile
import unittest

from scripts.v40_original_replay_interface import inspect_v40_source


class OriginalReplayInterfaceTests(unittest.TestCase):
    def test_static_source_only_and_cli_diagnostics(self):
        source = """
import argparse
from atr_floor_v16 import build_floor_pools
def chronological_replay(candidates): return candidates
def main(argv=None):
    ap=argparse.ArgumentParser()
    ap.add_argument('--initial-capital', type=float)
    ap.add_argument('--out', default='output')
    args=ap.parse_args(argv)
    return chronological_replay(build_floor_pools([], floors=(4.,)))
"""
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "contract_replay_v40_scale.py"
            path.write_text(source)
            r = inspect_v40_source(path)
            self.assertTrue(r["has_main"])
            self.assertTrue(r["has_chronological_replay_definition"])
            self.assertEqual(r["calls_build_floor_pools"], 1)
            self.assertTrue(r["has_initial_capital_flag"])
            self.assertIn("--out", r["cli_flags"])
            self.assertTrue(r["imports_candidate_pool_function"])
            self.assertFalse(r["source_executed"])
            self.assertFalse(r["portfolio_replay_performed"])
            self.assertFalse(r["live_authorization"])
            self.assertFalse(r["mexc_orders_sent"])
            self.assertNotIn("floors=(4.,)", str(r))

    def test_missing_source_fails(self):
        with self.assertRaisesRegex(ValueError, "RUNNER_NOT_FOUND_OR_TOO_LARGE"):
            inspect_v40_source(Path("/nonexistent_v40_source.py"))


if __name__ == "__main__":
    unittest.main()
