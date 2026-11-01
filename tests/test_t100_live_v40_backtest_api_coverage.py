"""Offline V40 entry-symbol/current MEXC public eligibility coverage tests.

No API credentials, network, trading orders, Render or data extraction.
"""
from __future__ import annotations

import csv
import io
import json
from pathlib import Path
import tempfile
import unittest
from zipfile import ZipFile

from scripts.v40_backtest_api_coverage import (
    HistoricalEntries, compare_current_support, read_entries, scenario_label,
)


def row(symbol, api_allowed=True, **fields):
    record = {
        "symbol": symbol, "apiAllowed": api_allowed, "state": 0,
        "futureType": 1, "positionOpenType": 3,
        "quoteCoin": "USDT", "settleCoin": "USDT",
        "riskLimitMode": "INCREASE", "riskLimitType": "BY_VOLUME",
    }
    record.update(fields)
    return record


def envelope(records):
    return {"success": True, "code": 0, "data": records}


def csv_data(records, columns):
    b = io.StringIO()
    writer = csv.DictWriter(b, fieldnames=columns)
    writer.writeheader()
    writer.writerows(records)
    return b.getvalue()


class V40BacktestApiCoverageTests(unittest.TestCase):
    def test_unique_and_entry_weighted_coverage_with_unknown_and_delisted(self):
        entries = HistoricalEntries(
            capital=2000, cost_bps=100, mode="HYBRID",
            scenario=scenario_label(100, "HYBRID"), final_equity=14476.81,
            symbols=("AAA_USDT", "AAA_USDT", "BBB_USDT", "CCC_USDT",
                     "DDD_USDT", "EEE_USDT"),
        )
        first = envelope([
            row("AAA_USDT"), row("BBB_USDT", api_allowed=False),
            row("CCC_USDT", api_allowed=None),
            row("DDD_USDT", api_allowed=True, positionOpenType=1),
            row("ZZZ_USDT"),
        ])
        second = envelope(list(reversed(first["data"])))
        report = compare_current_support(entries, first, second)
        self.assertEqual(report["backtest_entry_rows"], 6)
        self.assertEqual(report["unique_traded_contracts"], 5)
        self.assertEqual(report["currently_eligible_unique"], 1)
        self.assertEqual(report["currently_eligible_unique_pct"], 20.)
        self.assertEqual(report["currently_eligible_entries"], 2)
        self.assertEqual(report["currently_eligible_entries_pct"], 33.33)
        self.assertEqual(report["api_allowed_flag_unique"], 2)
        self.assertEqual(report["unique_breakdown"]["API_DENIED_NOW"], 1)
        self.assertEqual(report["unique_breakdown"]["API_FLAG_UNKNOWN"], 1)
        self.assertEqual(report["unique_breakdown"]["UNLISTED_NOW"], 1)
        self.assertEqual(
            report["unique_breakdown"]["API_ALLOWED_OTHER_RESTRICTION"], 1)
        self.assertFalse(report["historical_api_permissions_certified"])
        self.assertFalse(report["backtest_return_after_filter_replayed"])
        self.assertFalse(report["entry_allowed"])
        self.assertFalse(report["live_authorization"])
        self.assertFalse(report["mexc_orders_sent"])
        self.assertNotIn("AAA_USDT", str(report))

    def test_public_metadata_drift_blocks_complete_claim(self):
        entries = HistoricalEntries(2000, 100, "HYBRID", "H", 14476.81,
                                    ("AAA_USDT",))
        before = envelope([row("AAA_USDT"), row("BBB_USDT")])
        after = envelope([row("AAA_USDT", api_allowed=False), row("BBB_USDT")])
        with self.assertRaisesRegex(ValueError, "CURRENT_PUBLIC_METADATA_DRIFT"):
            compare_current_support(entries, before, after)

    def test_singleton_and_bad_public_envelopes_fail_closed(self):
        entries = HistoricalEntries(2000, 100, "HYBRID", "H", 14476.81,
                                    ("AAA_USDT",))
        for bad in (envelope([row("AAA_USDT")]),
                    {"success": False, "code": 5, "data": []},
                    envelope([row("AAA_USDT"), row("AAA_USDT")])):
            with self.subTest(bad=bad):
                with self.assertRaises(ValueError):
                    compare_current_support(entries, bad, bad)

    def test_exact_executed_scenario_rows_not_all_candidates(self):
        with tempfile.TemporaryDirectory() as tmp:
            zip_path = Path(tmp) / "v40_parity_for_live.zip"
            scenario = scenario_label(100, "HYBRID")
            other = scenario_label(100, "REFERENCE")
            with ZipFile(zip_path, "w") as z:
                z.writestr("manifest.json", json.dumps({
                    "type": "READ_ONLY_SOURCE_PARITY_SAMPLE",
                    "candidate_count": 146,
                }))
                z.writestr("capital_2000/entries.csv", csv_data([
                    {"scenario": scenario, "symbol": "AAA_USDT"},
                    {"scenario": scenario, "symbol": "BBB_USDT"},
                    {"scenario": other, "symbol": "CCC_USDT"},
                ], ["scenario", "symbol"]))
                z.writestr("capital_2000/metrics.csv", csv_data([
                    {"scenario": scenario, "entered": 2, "final_equity": 14476.81},
                    {"scenario": other, "entered": 1, "final_equity": 11000},
                ], ["scenario", "entered", "final_equity"]))
            entries = read_entries(zip_path)
            self.assertEqual(entries.symbols, ("AAA_USDT", "BBB_USDT"))
            self.assertEqual(entries.scenario, scenario)
            self.assertAlmostEqual(entries.final_equity / entries.capital,
                                   7.238405)
            self.assertEqual(len(entries.symbols), 2)
            with self.assertRaisesRegex(ValueError, "UNSUPPORTED_CAPITAL"):
                read_entries(zip_path, capital=5000)


if __name__ == "__main__":
    unittest.main()
