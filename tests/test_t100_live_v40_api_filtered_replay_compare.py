"""Offline reconciliation tests for pinned V40 baseline vs filtered replay output."""
from __future__ import annotations

import csv
import hashlib
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from zipfile import ZipFile

from scripts import v40_api_filtered_original_runner as pinned
from scripts.v40_api_filtered_replay_compare import compare_replays
from scripts.v40_backtest_api_coverage import scenario_label


def csv_data(rows, columns):
    stream = io.StringIO()
    writer = csv.DictWriter(stream, fieldnames=columns)
    writer.writeheader()
    writer.writerows(rows)
    return stream.getvalue()


class ReplayCompareTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.scenario = scenario_label(100, "HYBRID")
        self.symbols = [f"TEST{i:03d}_USDT" for i in range(111)]
        self.pool = self.symbols + self.symbols[:35]
        self.eligible = self.symbols[:98]
        self.denied = self.symbols[98:]
        self.entries = self.pool[:108]
        self.archive = self.root / "v40.zip"
        with ZipFile(self.archive, "w") as z:
            z.writestr("manifest.json", json.dumps({
                "type": "READ_ONLY_SOURCE_PARITY_SAMPLE", "candidate_count": 146,
            }))
            z.writestr("candidates.csv", csv_data([
                {"symbol": s, "entry_ts": str(i * 1800 + 3600000)}
                for i, s in enumerate(self.pool)
            ], ["symbol", "entry_ts"]))
            z.writestr("capital_2000/entries.csv", csv_data([
                {"scenario": self.scenario, "symbol": s}
                for s in self.entries
            ], ["scenario", "symbol"]))
            z.writestr("capital_2000/metrics.csv", csv_data([
                {"scenario": self.scenario, "entered": "108",
                 "final_equity": "14476.81238416"}
            ], ["scenario", "entered", "final_equity"]))
        sha = hashlib.sha256(self.archive.read_bytes()).hexdigest()
        self.manifest = self.root / "manifest.json"
        self.manifest.write_text(json.dumps({
            "type": "V40_CURRENT_PUBLIC_API_CANDIDATE_PREFILTER",
            "archive_sha256": sha,
            "scenario": self.scenario,
            "candidate_count": 146,
            "candidate_unique_contracts": 111,
            "candidate_symbols_current_public_eligible": self.eligible,
            "candidate_symbols_current_public_ineligible": self.denied,
        }))
        self.sha = sha
        self.baseline = self.root / "baseline"
        self.filtered = self.root / "filtered"
        self.baseline.mkdir()
        self.filtered.mkdir()

    def write(self, folder, symbols, equity, dd):
        (folder / "entries.csv").write_text(csv_data([
            {"scenario": self.scenario, "symbol": s}
            for s in symbols
        ], ["scenario", "symbol"]))
        (folder / "metrics.csv").write_text(csv_data([
            {"scenario": self.scenario, "entered": str(len(symbols)),
             "final_equity": str(equity), "high_envelope_dd_pct": str(dd)}
        ], ["scenario", "entered", "final_equity", "high_envelope_dd_pct"]))

    def test_reconciles_exact_baseline_and_filtered_supported_entries(self):
        self.write(self.baseline, self.entries, 14476.81238416, -54.8664)
        self.write(self.filtered,
                   [s for s in self.entries if s in set(self.eligible)],
                   11000., -40.5)
        with patch.object(pinned, "PINNED_ARCHIVE_SHA", self.sha):
            result = compare_replays(
                self.baseline, self.filtered, self.archive, self.manifest)
        self.assertTrue(result["baseline_trades_and_equity_reproduced"])
        self.assertEqual(result["original_baseline_entries"], 108)
        self.assertEqual(result["filtered_entries"], 98)
        self.assertEqual(result["filtered_equity_multiple"], 5.5)
        self.assertEqual(result["filtered_high_envelope_dd_pct"], -40.5)
        self.assertFalse(result["live_authorization"])

    def test_denied_symbols_and_baseline_drift_block(self):
        self.write(self.baseline, self.entries, 14476.81238416, -54.8)
        self.write(self.filtered, ["TEST110_USDT"], 10000., -20)
        with patch.object(pinned, "PINNED_ARCHIVE_SHA", self.sha):
            with self.assertRaisesRegex(ValueError, "FILTERED_ENTRY_INELIGIBLE_SYMBOL"):
                compare_replays(
                    self.baseline, self.filtered, self.archive, self.manifest)
        self.write(self.baseline, self.entries, 14460., -54.8)
        with patch.object(pinned, "PINNED_ARCHIVE_SHA", self.sha):
            with self.assertRaisesRegex(ValueError, "BASELINE_EQUITY_NOT_REPRODUCED"):
                compare_replays(
                    self.baseline, self.filtered, self.archive, self.manifest)


if __name__ == "__main__":
    unittest.main()
