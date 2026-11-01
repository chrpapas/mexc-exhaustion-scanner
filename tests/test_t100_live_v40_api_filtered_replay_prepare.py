"""Offline unit tests for V40 current-public pre-replay cohort preparation."""
from __future__ import annotations

import csv
import io
import json
from pathlib import Path
import tempfile
import unittest
from zipfile import ZipFile

from scripts.v40_api_filtered_replay_prepare import (
    prepare_manifest, write_new_local_manifest,
)


def make_csv(rows, fields):
    stream = io.StringIO()
    writer = csv.DictWriter(stream, fieldnames=fields)
    writer.writeheader()
    writer.writerows(rows)
    return stream.getvalue()


def snapshot(symbols):
    return {"success": True, "code": 0, "data": [
        {"symbol": s, "apiAllowed": True, "state": 0, "futureType": 1,
         "positionOpenType": 3, "quoteCoin": "USDT", "settleCoin": "USDT"}
        for s in symbols
    ]}


def archive(path):
    scenario = "V32_VOL30_SKIP_H4_S100_CAP5_V39_HYBRID"
    symbols = [f"TEST{i:03d}_USDT" for i in range(146)]
    with ZipFile(path, "w") as z:
        z.writestr("manifest.json", json.dumps({
            "type": "READ_ONLY_SOURCE_PARITY_SAMPLE",
            "candidate_count": 146,
        }))
        z.writestr("candidates.csv", make_csv([
            {"symbol": symbol, "entry_ts": str(100_000_000 + 1800 * i)}
            for i, symbol in enumerate(symbols)
        ], ["symbol", "entry_ts"]))
        z.writestr("capital_2000/entries.csv", make_csv([
            {"scenario": scenario, "symbol": symbol}
            for symbol in symbols[:108]
        ], ["scenario", "symbol"]))
        z.writestr("capital_2000/metrics.csv", make_csv([
            {"scenario": scenario, "entered": 108,
             "final_equity": "14476.81238416"}
        ], ["scenario", "entered", "final_equity"]))
    return symbols


class ApiPrefilterPreparationTests(unittest.TestCase):
    def test_filters_146_candidate_cohort_not_only_original_108_entries(self):
        with tempfile.TemporaryDirectory() as tmp:
            archive_path = Path(tmp) / "v40.zip"
            symbols = archive(archive_path)
            # 11 originally executed symbols and 4 initially unexecuted symbols
            # currently unavailable. Filtering only original entries would miss
            # the last four (which can matter after slots are freed).
            supported = [s for i, s in enumerate(symbols)
                         if i not in set(range(97, 108)) | set(range(140, 144))]
            public = snapshot(supported)
            manifest, report = prepare_manifest(
                archive_path, public, public, require_pinned=False)
            self.assertEqual(report["baseline_current_public_eligible_entries"], 97)
            self.assertEqual(report["baseline_current_public_ineligible_entries"], 11)
            self.assertEqual(report["candidate_distinct_public_ineligible"], 15)
            self.assertEqual(report["candidate_distinct_public_eligible"], 131)
            self.assertEqual(manifest["candidate_count"], 146)
            self.assertEqual(len(manifest["candidate_symbols_current_public_ineligible"]), 15)
            self.assertFalse(report["chronological_portfolio_replayed"])
            self.assertIsNone(report["filtered_equity_multiple"])
            self.assertFalse(report["live_authorization"])
            self.assertNotIn("TEST097_USDT", str(report))
            output = Path(tmp) / "filter.json"
            write_new_local_manifest(output, manifest)
            self.assertEqual(json.loads(output.read_text()), manifest)
            with self.assertRaises(FileExistsError):
                write_new_local_manifest(output, manifest)

    def test_public_metadata_drift_fails_closed(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "v40.zip"
            symbols = archive(path)
            good = snapshot(symbols)
            bad = snapshot(symbols)
            bad["data"][0]["apiAllowed"] = False
            with self.assertRaisesRegex(ValueError, "PUBLIC_CONTRACT_METADATA_DRIFT"):
                prepare_manifest(path, good, bad, require_pinned=False)

    def test_pinned_sha_mismatch_fails_closed(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "v40.zip"
            symbols = archive(path)
            good = snapshot(symbols)
            with self.assertRaisesRegex(ValueError, "FROZEN_ARCHIVE_SHA_MISMATCH"):
                prepare_manifest(path, good, good, require_pinned=True)


if __name__ == "__main__":
    unittest.main()
