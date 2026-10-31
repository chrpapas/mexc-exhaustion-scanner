"""Redacted current-TP/SL topology research only: no MEXC or DB calls."""
from __future__ import annotations
import json
import unittest

from app.t100_live_four_slot_stop_count_diagnostic import (
    classify_current_position_stop_mismatch,
)


class StopTopologyTests(unittest.TestCase):
    def setUp(self):
        self.positions = [
            {"positionId": "private-P1", "symbol": "SECRET_USDT", "holdVol": "2"},
        ]
        self.stop = {
            "id": "private-S1", "positionId": "private-P1",
            "symbol": "SECRET_USDT", "stopLossPrice": "150.0",
            "takeProfitPrice": "90.0",
        }

    def assert_blocked_redacted(self, result):
        flattened = json.dumps(result)
        for sensitive in ("private-P1", "private-S1", "SECRET_USDT",
                          "150.0", "90.0"):
            self.assertNotIn(sensitive, flattened)
        self.assertFalse(result["native_stop_coverage_certified"])
        self.assertFalse(result["live_authorization"])
        self.assertFalse(result["entry_allowed"])
        self.assertFalse(result["mexc_orders_sent"])
        self.assertTrue(result["read_only"])

    def test_no_stop_records_for_position(self):
        r = classify_current_position_stop_mismatch(self.positions, [])
        self.assertEqual(r["position_count"], 1)
        self.assertEqual(r["current_tpsl_row_count"], 0)
        self.assertEqual(r["positions_without_any_current_tpsl"], 1)
        self.assertEqual(r["positions_without_positive_stop_loss_row"], 1)
        self.assert_blocked_redacted(r)

    def test_two_current_records_one_position_including_tp_only(self):
        second = {
            **self.stop,
            "id": "private-S2",
            "stopLossPrice": None,
        }
        r = classify_current_position_stop_mismatch(
            self.positions, [self.stop, second],
        )
        self.assertEqual(r["position_count"], 1)
        self.assertEqual(r["current_tpsl_row_count"], 2)
        self.assertEqual(r["positions_with_multiple_current_tpsl_rows"], 1)
        self.assertEqual(r["current_tpsl_rows_without_positive_stop_loss"], 1)
        self.assertEqual(r["positions_without_positive_stop_loss_row"], 0)
        self.assertEqual(r["duplicate_current_tpsl_order_ids"], 0)
        self.assert_blocked_redacted(r)

    def test_single_take_profit_only_no_positive_stop_loss(self):
        tp = {**self.stop, "stopLossPrice": "0"}
        r = classify_current_position_stop_mismatch(self.positions, [tp])
        self.assertEqual(r["positions_without_any_current_tpsl"], 0)
        self.assertEqual(r["positions_without_positive_stop_loss_row"], 1)
        self.assert_blocked_redacted(r)

    def test_orphan_and_missing_position_id_stop(self):
        missing = {**self.stop, "id": "private-S2", "positionId": None}
        orphan = {**self.stop, "id": "private-S3", "positionId": "unknown"}
        r = classify_current_position_stop_mismatch(
            self.positions, [self.stop, missing, orphan],
        )
        self.assertEqual(r["current_tpsl_rows_with_unmatched_position"], 1)
        self.assertEqual(r["current_tpsl_rows_without_position_id"], 1)
        self.assertEqual(r["positions_with_multiple_current_tpsl_rows"], 0)
        self.assert_blocked_redacted(r)

    def test_stop_without_open_position(self):
        r = classify_current_position_stop_mismatch([], [self.stop])
        self.assertEqual(r["position_count"], 0)
        self.assertEqual(r["current_tpsl_row_count"], 1)
        self.assertEqual(r["current_tpsl_rows_with_unmatched_position"], 1)
        self.assert_blocked_redacted(r)

    def test_duplicate_stop_ids_not_accepted_as_unique(self):
        x = dict(self.stop)
        r = classify_current_position_stop_mismatch(self.positions, [x, x])
        self.assertEqual(r["duplicate_current_tpsl_order_ids"], 1)
        self.assert_blocked_redacted(r)

    def test_duplicate_position_identity_refuses_to_classify(self):
        r = classify_current_position_stop_mismatch(
            self.positions * 2, [self.stop],
        )
        self.assertEqual(r["status"], "POSITION_STOP_TOPOLOGY_UNVERIFIED")
        self.assertTrue(r["position_ids_ambiguous"])
        self.assertIsNone(r["positions_without_any_current_tpsl"])
        self.assert_blocked_redacted(r)

    def test_invalid_schema_never_guesses(self):
        for bad in (None, {"data": []}, "[]", [{"positionId": "x"}, None]):
            with self.subTest(bad=bad):
                r = classify_current_position_stop_mismatch(bad, [])
                self.assertEqual(r["status"], "POSITION_STOP_TOPOLOGY_UNVERIFIED")
                self.assertIsNone(r["position_count"])
                self.assert_blocked_redacted(r)


if __name__ == "__main__":
    unittest.main()
