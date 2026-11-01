"""Offline historical V40 pre-lot capacity regression tests (mock records)."""
from __future__ import annotations

from datetime import datetime, timezone, timedelta
from decimal import Decimal as D
import json
from pathlib import Path
import tempfile
import unittest
from zipfile import ZipFile, ZIP_DEFLATED

from app.t100_live_v40_historical_capacity import (
    assess_historical_v40_entry_capacity as assess,
)
from scripts.v40_historical_capacity_audit import (
    audit, load_entry_ledger,
)
from tests.test_t100_live_public_v40_capacity import siren


_TIME = "2025-06-01T12:00:00Z"


def entry(target="6250", saved="500", time=_TIME, symbol="SIREN_USDT"):
    return dict(
        symbol=symbol, time=time,
        desired_before_liquidity=target,
        prior_turnover_quota_usdt="10000",
        p25_turnover="200000",
        exit_capacity_limit="10000",
        notional=saved,
    )


def witness(time=_TIME, price="0.02298", contract=None,
            contract_at="2025-06-01T11:00:00Z",
            price_at="2025-06-01T11:59:00Z"):
    return dict(
        symbol="SIREN_USDT", time=time,
        entry_reference_price=price,
        price_observed_at=price_at,
        contract_observed_at=contract_at,
        contract=siren() if contract is None else contract,
    )


class HistoricalV40EntryCapacityTests(unittest.TestCase):
    def test_reject_pre_lot_model_even_when_clipped_export_fits(self):
        result = assess(entry(), witness())
        self.assertEqual(
            result.status, "V40_HISTORICAL_PUBLIC_POSITION_CAP_REJECT",
        )
        self.assertEqual(result.pre_lot_reference_notional_usdt, D("6250"))
        self.assertEqual(result.exported_entry_notional_usdt, D("500"))
        self.assertTrue(result.exported_entry_below_reference)
        self.assertGreater(result.lot_count_for_reference, D("3600"))
        self.assertFalse(result.public_position_tier_fits)
        self.assertFalse(result.historical_execution_capacity_certified)
        self.assertFalse(result.entry_allowed)
        self.assertFalse(result.live_authorization)

    def test_current_public_cap_can_fit_small_reference_not_live(self):
        result = assess(entry(target="500", saved="499"), witness())
        self.assertEqual(
            result.status, "V40_HISTORICAL_PUBLIC_CAP_FITS_NONAUTHORIZING",
        )
        self.assertTrue(result.public_position_tier_fits)
        self.assertTrue(result.public_single_order_fits)
        self.assertFalse(result.point_in_time_venue_authenticity_certified)
        self.assertFalse(result.private_tier_verified)
        self.assertFalse(result.liquidity_depth_verified)
        self.assertFalse(result.live_authorization)

    def test_without_archived_contract_or_price_never_fallback_to_today(self):
        row = entry()
        self.assertEqual(
            assess(row, None).status,
            "V40_HISTORICAL_POINT_IN_TIME_EVIDENCE_MISSING",
        )
        for bad, expected in (
            (dict(witness(), contract_observed_at="2026-10-30T12:00:00Z"),
             "V40_HISTORICAL_CONTRACT_SNAPSHOT_NOT_POINT_IN_TIME"),
            (dict(witness(), contract_observed_at="2025-05-01T12:00:00Z"),
             "V40_HISTORICAL_CONTRACT_SNAPSHOT_NOT_POINT_IN_TIME"),
            (dict(witness(), price_observed_at="2025-06-01T12:01:00Z"),
             "V40_HISTORICAL_PRICE_NOT_POINT_IN_TIME"),
            (dict(witness(), price_observed_at="2025-06-01T11:00:00Z"),
             "V40_HISTORICAL_PRICE_NOT_POINT_IN_TIME"),
            (dict(witness(), entry_reference_price="NaN"),
             "V40_HISTORICAL_PRICE_INVALID"),
            (dict(witness(), contract={}),
             "V40_HISTORICAL_CONTRACT_IDENTITY_INVALID"),
            (dict(witness(), time="2025-06-01T12:30:00Z"),
             "V40_HISTORICAL_EVIDENCE_IDENTITY_INVALID"),
        ):
            with self.subTest(expected=expected):
                self.assertEqual(assess(row, bad).status, expected)

    def test_exported_notional_above_reconstructed_reference_fails_closed(self):
        result = assess(entry(target="500", saved="501"), witness())
        self.assertEqual(
            result.status,
            "V40_HISTORICAL_ENTRY_REFERENCE_RECONSTRUCTION_DRIFT",
        )
        self.assertFalse(result.point_in_time_evidence_supplied)

    def test_liquidity_bound_is_respected_not_gross_slot_target(self):
        row = entry(target="6250", saved="200")
        row["prior_turnover_quota_usdt"] = "200"
        result = assess(row, witness())
        self.assertEqual(result.pre_lot_reference_notional_usdt, D("200"))
        self.assertEqual(
            result.status, "V40_HISTORICAL_PUBLIC_CAP_FITS_NONAUTHORIZING",
        )


class FrozenArchiveBatchTests(unittest.TestCase):
    def _make_fixture_archive(self, path: Path):
        first = datetime(2025, 6, 1, 12, tzinfo=timezone.utc)
        def time(i):
            return (first + timedelta(minutes=30*i)).isoformat()
        def csv_rows(fieldnames, rows):
            import csv, io
            out = io.StringIO()
            w = csv.DictWriter(out, fieldnames=fieldnames)
            w.writeheader()
            w.writerows(rows)
            return out.getvalue()
        candidates = [
            {"symbol": f"COIN{i}_USDT",
             "entry_ts": str(int((first+timedelta(minutes=30*i)).timestamp()))}
            for i in range(146)
        ]
        # Include the concrete, tested signal with its archived name.
        candidates[0]["symbol"] = "SIREN_USDT"
        gates = [
            {"scenario": "V32_VOL30_SKIP_H4_S100_CAP5_V39_HYBRID",
             "symbol": c["symbol"], "time": time(i), "action": "ADMIT"}
            for i, c in enumerate(candidates)
        ]
        firstentry = entry(symbol="SIREN_USDT", time=time(0))
        second = entry(target="500", saved="499",
                       symbol="COIN1_USDT", time=time(1))
        for row in (firstentry, second):
            row["scenario"] = "V32_VOL30_SKIP_H4_S100_CAP5_V39_HYBRID"
        with ZipFile(path, "w", ZIP_DEFLATED) as z:
            z.writestr("manifest.json", json.dumps({
                "type": "READ_ONLY_SOURCE_PARITY_SAMPLE",
                "candidate_count": 146,
                "preceding_candles_exported": 7154,
            }))
            z.writestr("candidates.csv", csv_rows(
                ("symbol", "entry_ts"), candidates,
            ))
            z.writestr("capital_10000/gate_decisions.csv", csv_rows(
                ("scenario", "symbol", "time", "action"), gates,
            ))
            z.writestr("capital_10000/entries.csv", csv_rows(
                ("scenario", "symbol", "time", "notional",
                 "desired_before_liquidity", "prior_turnover_quota_usdt",
                 "p25_turnover", "exit_capacity_limit"),
                (firstentry, second),
            ))

    def test_batch_missing_evidence_is_unknown_and_local_details_optional(self):
        with tempfile.TemporaryDirectory() as tmp:
            folder = Path(tmp)
            archive = folder / "test.zip"
            detail = folder / "detail.json"
            template = folder / "template.jsonl"
            self._make_fixture_archive(archive)
            entries = load_entry_ledger(
                archive, capital=10000, cost_bps=100, mode="HYBRID",
            )
            self.assertEqual(len(entries), 2)
            report = audit(
                archive, output_details=detail, template_output=template,
                require_pinned_archive=False,
            )
            self.assertEqual(report["entries_checked"], 2)
            self.assertEqual(
                report["status"], "V40_HISTORICAL_CAPACITY_EVIDENCE_INCOMPLETE",
            )
            self.assertEqual(
                report["outcome_counts"],
                {"V40_HISTORICAL_POINT_IN_TIME_EVIDENCE_MISSING": 2},
            )
            self.assertNotIn("SIREN_USDT", json.dumps(report))
            self.assertIn("SIREN_USDT", detail.read_text())
            self.assertIn("contract_observed_at", template.read_text())
            self.assertFalse(report["live_authorization"])

    def test_real_archive_requires_pinned_sha_before_any_classification(self):
        with tempfile.TemporaryDirectory() as tmp:
            archive = Path(tmp) / "untrusted.zip"
            self._make_fixture_archive(archive)
            with self.assertRaisesRegex(ValueError, "HASH_NOT_PINNED"):
                audit(archive)

    def test_unmatched_evidence_rejected_not_applied_to_other_signal(self):
        with tempfile.TemporaryDirectory() as tmp:
            folder = Path(tmp)
            archive = folder / "test.zip"
            proof = folder / "proof.jsonl"
            self._make_fixture_archive(archive)
            proof.write_text(json.dumps(dict(witness(), time="2025-06-02T12:00:00Z"))+"\n")
            with self.assertRaisesRegex(ValueError, "NONENTRY"):
                audit(archive, evidence_jsonl=proof,
                      require_pinned_archive=False)

    def test_no_runtime_network_order_or_mexc_auth_import(self):
        import inspect
        import scripts.v40_historical_capacity_audit as runner
        import app.t100_live_v40_historical_capacity as evaluator
        content = inspect.getsource(runner) + inspect.getsource(evaluator)
        for item in (
            "MexcTradeClient(", "._private_post(", "asyncpg.",
            "LiveMutationPermit", ".send_one(", "httpx.",
        ):
            self.assertNotIn(item, content)


if __name__ == "__main__":
    unittest.main()
