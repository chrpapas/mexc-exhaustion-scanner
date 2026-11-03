"""Offline semantic checks for current MEXC TP/SL rows."""
import unittest

from app.t100_live_active_tpsl import assess_active_tpsl_semantics


def pos(**extra):
    row = {
        "symbol": "SIREN_USDT",
        "positionId": 10,
        "positionType": 2,
        "holdVol": 5,
        "state": 1,
    }
    row.update(extra)
    return row


def stop(**extra):
    row = {
        "id": 99,
        "symbol": "SIREN_USDT",
        "positionId": "10",
        "positionType": 2,
        "state": 1,
        "isFinished": 0,
        "stopLossPrice": 175.0,
        "vol": 5,
        "profitLossVolType": "SAME",
    }
    row.update(extra)
    return row


class ActiveTpslTests(unittest.TestCase):
    def test_matching_current_protection_rows_are_semantically_compatible(self):
        r = assess_active_tpsl_semantics([pos()], [stop()])
        self.assertTrue(r["active_tpsl_row_semantics_verified"])
        self.assertTrue(r["all_rows_link_to_observed_position"])
        self.assertTrue(r["observed_stop_loss_volume_covers_positions"])
        self.assertFalse(r["endpoint_completeness_verified"])
        self.assertFalse(r["exchange_atomicity_verified"])

    def test_string_position_id_matches_integer_position_id(self):
        r = assess_active_tpsl_semantics([pos()], [stop(positionId="10")])
        self.assertTrue(r["all_rows_link_to_observed_position"])

    def test_state_must_be_untriggered(self):
        r = assess_active_tpsl_semantics([pos()], [stop(state=2)])
        self.assertFalse(r["all_rows_state_untriggered"])
        self.assertFalse(r["active_tpsl_row_semantics_verified"])

    def test_is_finished_field_is_required_and_zero(self):
        s = stop()
        del s["isFinished"]
        self.assertFalse(
            assess_active_tpsl_semantics([pos()], [s])[
                "all_rows_nonfinal_if_field_present"
            ]
        )
        self.assertFalse(
            assess_active_tpsl_semantics([pos()], [stop(isFinished=1)])[
                "active_tpsl_row_semantics_verified"
            ]
        )

    def test_orphan_stop_blocks(self):
        r = assess_active_tpsl_semantics([pos()], [stop(positionId="999")])
        self.assertFalse(r["all_rows_link_to_observed_position"])
        self.assertFalse(r["every_observed_position_has_current_tpsl"])

    def test_symbol_mismatch_blocks(self):
        r = assess_active_tpsl_semantics([pos()], [stop(symbol="OTHER_USDT")])
        self.assertFalse(r["all_rows_symbol_link_consistent"])

    def test_side_mismatch_blocks(self):
        r = assess_active_tpsl_semantics([pos()], [stop(positionType=1)])
        self.assertFalse(r["all_rows_position_side_consistent_if_field_present"])

    def test_missing_side_blocks_stronger_semantics(self):
        s = stop()
        del s["positionType"]
        r = assess_active_tpsl_semantics([pos()], [s])
        self.assertFalse(r["all_rows_position_side_consistent_if_field_present"])

    def test_nonpositive_stop_price_blocks(self):
        r = assess_active_tpsl_semantics([pos()], [stop(stopLossPrice=0)])
        self.assertFalse(r["all_rows_positive_stop_loss_price"])

    def test_nonpositive_volume_blocks(self):
        r = assess_active_tpsl_semantics([pos()], [stop(vol=0)])
        self.assertFalse(r["all_rows_positive_protective_volume"])

    def test_entire_position_same_quantity_can_infer_dynamic_coverage(self):
        r = assess_active_tpsl_semantics(
            [pos(holdVol=5)],
            [
                stop(
                    vol=0,
                    volType=2,
                    profitLossVolType="SAME",
                )
            ],
        )
        self.assertFalse(r["all_rows_positive_protective_volume"])
        self.assertTrue(
            r["all_rows_effective_protective_quantity_verified"]
        )
        self.assertTrue(r["observed_stop_loss_volume_covers_positions"])
        self.assertTrue(r["active_tpsl_row_semantics_verified"])

    def test_entire_position_separate_quantity_still_requires_stop_loss_volume(self):
        r = assess_active_tpsl_semantics(
            [pos(holdVol=5)],
            [
                stop(
                    vol=0,
                    volType=2,
                    profitLossVolType="SEPARATE",
                    stopLossVol=0,
                )
            ],
        )
        self.assertFalse(
            r["all_rows_effective_protective_quantity_verified"]
        )
        self.assertFalse(r["observed_stop_loss_volume_covers_positions"])
        self.assertFalse(r["active_tpsl_row_semantics_verified"])

    def test_separate_quantity_uses_stop_loss_volume(self):
        r = assess_active_tpsl_semantics(
            [pos(holdVol=5)],
            [stop(profitLossVolType="SEPARATE", vol=1, stopLossVol=5)],
        )
        self.assertTrue(r["observed_stop_loss_volume_covers_positions"])

    def test_partial_stop_volume_does_not_cover_position(self):
        r = assess_active_tpsl_semantics([pos(holdVol=5)], [stop(vol=4)])
        self.assertFalse(r["observed_stop_loss_volume_covers_positions"])
        self.assertFalse(r["active_tpsl_row_semantics_verified"])

    def test_multiple_rows_can_cover_position(self):
        r = assess_active_tpsl_semantics(
            [pos(holdVol=5)],
            [stop(id=1, vol=2), stop(id=2, vol=3)],
        )
        self.assertTrue(r["observed_stop_loss_volume_covers_positions"])

    def test_multiple_positions_each_need_coverage(self):
        p2 = pos(symbol="OTHER_USDT", positionId=11, holdVol=2)
        s2 = stop(id=100, symbol="OTHER_USDT", positionId="11", vol=2)
        r = assess_active_tpsl_semantics([pos(), p2], [stop(), s2])
        self.assertTrue(r["every_observed_position_has_current_tpsl"])
        self.assertTrue(r["observed_stop_loss_volume_covers_positions"])

    def test_empty_rows_are_insufficient(self):
        r = assess_active_tpsl_semantics([], [])
        self.assertEqual(r["status"], "INSUFFICIENT_OBSERVED_ROWS")
        self.assertFalse(r["active_tpsl_row_semantics_verified"])

    def test_malformed_input_fails_closed(self):
        r = assess_active_tpsl_semantics([None], [])
        self.assertEqual(r["status"], "INVALID_INPUT")

    def test_no_raw_values_are_returned(self):
        r = assess_active_tpsl_semantics(
            [pos(symbol="SECRET_USDT", positionId=123456789)],
            [stop(symbol="SECRET_USDT", positionId="123456789",
                  id=777, stopLossPrice=9876.54)],
        )
        for value in ("SECRET_USDT", "123456789", "9876.54", "777"):
            self.assertNotIn(value, str(r))
