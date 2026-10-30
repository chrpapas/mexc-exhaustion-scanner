"""Mocked local-only source inventory: never print symbol records or certify."""
from __future__ import annotations

import csv
import io
import json
from pathlib import Path
import tempfile
import unittest

from scripts.v40_local_contract_source_inventory import (
    _hint, inspect_source, survey,
)
from tests.test_t100_live_v40_historical_capacity import FrozenArchiveBatchTests


class LocalSourceInventoryTests(unittest.TestCase):
    def test_wayback_filename_is_hint_not_capture_proof(self):
        self.assertEqual(
            _hint("_cleanup/snapshot_20251111153736_contracts.json"),
            "2025-11-11T15:37:36+00:00",
        )
        self.assertIsNone(_hint("specs.json"))

    def test_symbol_keyed_json_does_not_leak_identifier_in_field_sample(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            path = root / "snapshot_20251111153736_contracts.json"
            path.write_text(json.dumps({
                "data": {
                    "SIREN_USDT": {"contractSize": 10, "maxVol": 3600},
                    "XEX_USDT": {"contractSize": 1},
                },
                "snapshot_at": "2025-11-11T15:37:36Z",
            }), encoding="utf-8")
            r = inspect_source(root, path.name, {"SIREN_USDT", "ELSE_USDT"})
            self.assertEqual(r["status"], "SCHEMA_SURVEYED_NONAUTHORIZING")
            self.assertEqual(r["distinct_entry_symbols_matched"], 1)
            self.assertEqual(r["symbols_discovered"], 2)
            self.assertEqual(
                r["capture_time_field_name_candidates"], ["snapshot_at"],
            )
            self.assertNotIn("SIREN_USDT", json.dumps(r))
            self.assertNotIn("XEX_USDT", json.dumps(r))
            self.assertFalse(r["historical_capture_provenance_certified"])
            self.assertFalse(r["historical_capacity_certified"])

    def test_csv_symbol_values_only_counted_not_printed(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            path = root / "specs.csv"
            path.write_text(
                "symbol,contractSize,captured_at\n"
                "SIREN_USDT,10,2025-06-01T11:00:00Z\n"
                "OTHER_USDT,5,2025-06-01T11:00:00Z\n",
                encoding="utf-8",
            )
            result = inspect_source(root, path.name, {"SIREN_USDT"})
            self.assertEqual(result["distinct_entry_symbols_matched"], 1)
            self.assertEqual(result["records_examined"], 2)
            self.assertNotIn("OTHER_USDT", str(result))
            self.assertNotIn("SIREN_USDT", str(result))
            self.assertFalse(result["historical_capture_provenance_certified"])

    def test_missing_and_malformed_sources_cannot_be_certified(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            absent = inspect_source(root, "absent.json", {"SIREN_USDT"})
            self.assertEqual(absent["status"], "FILE_NOT_FOUND")
            f = root / "broken.json"
            f.write_text("{", encoding="utf-8")
            malformed = inspect_source(root, f.name, {"SIREN_USDT"})
            self.assertEqual(malformed["status"], "SOURCE_PARSE_FAILED")
            escape = inspect_source(root, "../outside.json", {"SIREN_USDT"})
            self.assertEqual(escape["status"], "SOURCE_PARSE_FAILED")

    def test_miniature_frozen_archive_scan_and_sha_guard(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            archive = root / "archive.zip"
            FrozenArchiveBatchTests()._make_fixture_archive(archive)
            source = root / "historical.json"
            source.write_text(json.dumps([
                {"symbol": "SIREN_USDT", "contractSize": 10},
                {"symbol": "OUTSIDE_USDT", "contractSize": 2},
            ]), encoding="utf-8")
            result = survey(
                root, archive, relative_paths=("historical.json", "not_present.json"),
                require_pinned=False,
            )
            self.assertEqual(result["status"],
                             "V40_LOCAL_SOURCE_INVENTORY_NONAUTHORIZING")
            self.assertEqual(result["entries_in_frozen_hybrid"], 2)
            self.assertEqual(result["sources"][0]["distinct_entry_symbols_matched"], 1)
            self.assertEqual(result["sources"][1]["status"], "FILE_NOT_FOUND")
            self.assertFalse(result["point_in_time_contract_coverage_certified"])
            self.assertFalse(result["live_authorization"])
            self.assertNotIn("SIREN_USDT", str(result))

            blocked = survey(
                root, archive, relative_paths=("historical.json",),
                require_pinned=True,
            )
            self.assertEqual(blocked["status"], "V40_ARCHIVE_SHA_MISMATCH")

    def test_no_live_account_or_transport_import(self):
        import inspect
        import scripts.v40_local_contract_source_inventory as source
        code = inspect.getsource(source)
        for forbidden in (
            "MexcTradeClient", "._private_post(", "httpx.",
            "asyncpg.", "LiveMutationPermit", ".send_one(",
        ):
            self.assertNotIn(forbidden, code)


if __name__ == "__main__":
    unittest.main()
