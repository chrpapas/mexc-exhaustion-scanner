"""Offline tests for journal-correlated stop amendment recognition."""
import unittest
from decimal import Decimal as D

from app.t100_live_expected_stop_transition import (
    ExpectedStopTransition,
    assess_expected_stop_transition,
)


def expected():
    return ExpectedStopTransition.build(
        external_id="t100sabc",
        symbol="SIREN_USDT",
        position_id="10",
        previous_stop_order_id="99",
        previous_stop=D("175"),
        proposed_stop=D("90"),
    )


def stop(**extra):
    row = {
        "id": "99",
        "positionId": "10",
        "symbol": "SIREN_USDT",
        "state": 1,
        "isFinished": 0,
        "stopLossPrice": "175",
    }
    row.update(extra)
    return row


def finished(**extra):
    row = {
        "id": "99",
        "positionId": "10",
        "symbol": "SIREN_USDT",
        "state": 2,
        "triggerSide": 0,
        "isFinished": 1,
        "stopLossPrice": "175",
    }
    row.update(extra)
    return row


class ExpectedStopTransitionTests(unittest.TestCase):
    def test_in_place_tightening_is_expected(self):
        result = assess_expected_stop_transition(
            before_stops=[stop()],
            after_stops=[stop(stopLossPrice="90")],
            expected=expected(),
        )
        self.assertTrue(result["expected_transition_verified"])
        self.assertTrue(result["same_stop_identity"])
        self.assertEqual(
            result["status"],
            "EXPECTED_STOP_AMENDMENT_IN_PLACE_VERIFIED",
        )
        self.assertFalse(result["exchange_atomicity_verified"])
        self.assertFalse(result["live_authorization"])

    def test_protection_gap_is_never_accepted(self):
        result = assess_expected_stop_transition(
            before_stops=[stop()],
            after_stops=[],
            expected=expected(),
        )
        self.assertFalse(result["expected_transition_verified"])
        self.assertTrue(result["protection_gap_observed"])
        self.assertEqual(result["status"], "PROTECTION_GAP_NOT_ACCEPTED")

    def test_unexpected_price_does_not_match_journal(self):
        result = assess_expected_stop_transition(
            before_stops=[stop()],
            after_stops=[stop(stopLossPrice="120")],
            expected=expected(),
        )
        self.assertFalse(result["expected_transition_verified"])
        self.assertEqual(
            result["status"], "AFTER_STOP_NOT_EXPECTED_TIGHTENING"
        )

    def test_stale_previous_price_blocks(self):
        result = assess_expected_stop_transition(
            before_stops=[stop(stopLossPrice="170")],
            after_stops=[stop(stopLossPrice="90")],
            expected=expected(),
        )
        self.assertFalse(result["expected_transition_verified"])
        self.assertEqual(
            result["status"], "JOURNAL_PREVIOUS_STOP_PRICE_MISMATCH"
        )

    def test_replacement_requires_terminal_provenance(self):
        result = assess_expected_stop_transition(
            before_stops=[stop()],
            after_stops=[stop(id="100", stopLossPrice="90")],
            expected=expected(),
        )
        self.assertFalse(result["expected_transition_verified"])
        self.assertTrue(result["replacement_stop_identity"])
        self.assertEqual(
            result["status"],
            "STOP_REPLACEMENT_TERMINAL_PROVENANCE_REQUIRED",
        )

    def test_replacement_with_canceled_old_stop_is_expected(self):
        result = assess_expected_stop_transition(
            before_stops=[stop()],
            after_stops=[stop(id="100", stopLossPrice="89.5")],
            finished_stops=[finished()],
            expected=expected(),
        )
        self.assertTrue(result["expected_transition_verified"])
        self.assertTrue(result["replacement_stop_identity"])
        self.assertTrue(result["old_stop_terminal_cancel_verified"])
        self.assertEqual(
            result["status"],
            "EXPECTED_STOP_AMENDMENT_REPLACEMENT_VERIFIED",
        )

    def test_triggered_old_stop_cannot_explain_replacement(self):
        result = assess_expected_stop_transition(
            before_stops=[stop()],
            after_stops=[stop(id="100", stopLossPrice="90")],
            finished_stops=[finished(state=3, triggerSide=2)],
            expected=expected(),
        )
        self.assertFalse(result["expected_transition_verified"])
        self.assertEqual(
            result["status"], "OLD_STOP_TERMINAL_CANCEL_UNVERIFIED"
        )

    def test_loosen_or_equal_stop_is_rejected(self):
        for price in ("175", "176"):
            with self.subTest(price=price):
                result = assess_expected_stop_transition(
                    before_stops=[stop()],
                    after_stops=[stop(stopLossPrice=price)],
                    expected=expected(),
                )
                self.assertFalse(result["expected_transition_verified"])

    def test_duplicate_target_after_is_ambiguous(self):
        result = assess_expected_stop_transition(
            before_stops=[stop()],
            after_stops=[
                stop(stopLossPrice="90"),
                stop(id="100", stopLossPrice="90"),
            ],
            expected=expected(),
        )
        self.assertEqual(result["status"], "AFTER_STOP_NOT_UNIQUE")

    def test_no_raw_identifiers_or_prices_returned(self):
        exp = ExpectedStopTransition.build(
            external_id="SECRET_EXTERNAL_ID",
            symbol="VERY_SECRET_USDT",
            position_id="123456789",
            previous_stop_order_id="987654321",
            previous_stop=D("12345.67"),
            proposed_stop=D("12000.00"),
        )
        result = assess_expected_stop_transition(
            before_stops=[{
                "id": "987654321",
                "positionId": "123456789",
                "symbol": "VERY_SECRET_USDT",
                "state": 1,
                "isFinished": 0,
                "stopLossPrice": "12345.67",
            }],
            after_stops=[{
                "id": "987654321",
                "positionId": "123456789",
                "symbol": "VERY_SECRET_USDT",
                "state": 1,
                "isFinished": 0,
                "stopLossPrice": "12000.00",
            }],
            expected=exp,
        )
        rendered = str(result)
        for secret in (
            "SECRET_EXTERNAL_ID",
            "VERY_SECRET_USDT",
            "123456789",
            "987654321",
            "12345.67",
            "12000.00",
        ):
            self.assertNotIn(secret, rendered)


if __name__ == "__main__":
    unittest.main()
