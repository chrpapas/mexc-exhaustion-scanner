"""Regression for observed MEXC V5 triggerSide conflict. No exchange I/O."""
from __future__ import annotations

from decimal import Decimal as D
import unittest

from app.t100_live_native_stop_terminal_audit import audit_terminal_native_short_stop


def stop(**overrides):
    value = dict(id="123", orderId=0, positionId="321", symbol="BTC_USDT",
                 state=3, isFinished=1, positionType=2, stopLossType=0,
                 volType=2, stopLossReverse=2, lossTrend=1, errorCode=0,
                 profitLossVolType="SAME", stopLossPrice="70000",
                 realityVol="1", placeOrderId="789", triggerSide=1)
    value.update(overrides)
    return value


def close(**overrides):
    value = dict(orderId="789", positionId="321", symbol="BTC_USDT",
                 side=2, state=3, orderType=5, errorCode=0, dealVol="1")
    value.update(overrides)
    return value


def audit(s=None, c=None, **overrides):
    args = dict(stop=stop() if s is None else s,
                close_order=close() if c is None else c,
                expected_symbol="BTC_USDT", expected_position_id="321",
                expected_contracts=D("1"), expected_stop_price=D("70000"),
                active_stop_previously_verified=True,
                journal_fallback_post_absent=True,
                account_stably_flat=True)
    args.update(overrides)
    return audit_terminal_native_short_stop(**args)


class TerminalPilotEvidenceTests(unittest.TestCase):
    def test_observed_wrong_trigger_side_preserves_close_but_blocks_sl_cert(self):
        result = audit()
        self.assertEqual(result.status, "VENUE_TP_TRIGGER_SIDE_ON_MARKET_SL_CLOSE")
        self.assertTrue(result.venue_generated_close_filled)
        self.assertTrue(result.venue_trigger_side_conflict)
        self.assertFalse(result.stop_loss_trigger_proven)
        self.assertFalse(result.live_authorization)
        self.assertFalse(result.entry_allowed)

    def test_correct_side_with_filled_link_still_does_not_authorize_live(self):
        result = audit(s=stop(triggerSide=2))
        self.assertEqual(result.status, "VERIFIED_NATIVE_STOP_LOSS_TRIGGER_FILLED")
        self.assertTrue(result.stop_loss_trigger_proven)
        self.assertFalse(result.live_authorization)

    def test_filled_close_must_match_exact_exchange_stop_generated_order(self):
        for change in ({"orderId": "999"}, {"side": 4}, {"state": 2},
                       {"dealVol": "0.5"}, {"positionId": "456"},
                       {"orderType": 1}, {"errorCode": 1}):
            with self.subTest(change=change):
                self.assertFalse(audit(c=close(**change)).venue_generated_close_filled)

    def test_invalid_stop_and_partial_coverage_cannot_pass(self):
        for change in ({"state": 1}, {"stopLossType": 1}, {"volType": 1},
                       {"placeOrderId": "999"}, {"realityVol": "0.5"},
                       {"stopLossReverse": 1}, {"errorCode": 1}, {"isFinished": 0},
                       {"stopLossPrice": "60000"}, {"orderId": 111}):
            with self.subTest(change=change):
                self.assertFalse(audit(s=stop(**change)).stop_loss_trigger_proven)

    def test_missing_active_stop_fallback_uncertainty_or_not_flat_blocks(self):
        for change in ({"active_stop_previously_verified": False},
                       {"journal_fallback_post_absent": False},
                       {"account_stably_flat": False}):
            with self.subTest(change=change):
                self.assertFalse(audit(s=stop(triggerSide=2),
                                       **change).stop_loss_trigger_proven)

    def test_unknown_trigger_sides_do_not_certify(self):
        for side in (0, 3, None, "2", True):
            with self.subTest(side=side):
                self.assertFalse(audit(s=stop(triggerSide=side)).stop_loss_trigger_proven)

    def test_bad_untrusted_inputs_fail_closed(self):
        for change in ({"expected_contracts": D("NaN")},
                       {"expected_stop_price": D("Infinity")},
                       {"expected_position_id": "not-an-id"},
                       {"active_stop_previously_verified": 1}):
            with self.subTest(change=change):
                self.assertEqual(audit(**change).status, "TERMINAL_STOP_AUDIT_INPUT_INVALID")

    def test_real_v5_economics_consistent_but_venue_tp_conflict_blocks_cert(self):
        entry = dict(orderId="456", symbol="BTC_USDT", positionId="321",
                     side=3, state=3, dealVol="1", dealAvgPrice="69000")
        linked_close = close(dealAvgPrice="70100")
        result = audit(s=stop(takeProfitPrice=None),
                       c=linked_close, entry_order=entry,
                       reviewed_stop_only_request=True)
        self.assertEqual(result.status, "VENUE_TP_LABEL_CONFLICT_LOSS_DIRECTION_CLOSE")
        self.assertTrue(result.venue_generated_close_filled)
        self.assertTrue(result.stop_loss_economic_behavior_consistent)
        self.assertTrue(result.venue_trigger_side_conflict)
        self.assertFalse(result.stop_loss_trigger_proven)
        self.assertFalse(result.live_authorization)
        self.assertFalse(result.entry_allowed)

    def test_economic_consistency_cannot_be_faked_by_tp_or_profitable_exit(self):
        valid_entry = dict(orderId="456", symbol="BTC_USDT", positionId="321",
                           side=3, state=3, dealVol="1", dealAvgPrice="69000")
        for change in (
                dict(s=stop(takeProfitPrice="80000"), c=close(dealAvgPrice="70100")),
                dict(s=stop(), c=close(dealAvgPrice="68000")),
                dict(s=stop(), c=close(dealAvgPrice="69000")),
                dict(s=stop(), c=close(dealAvgPrice=None)),
                dict(s=stop(), c=close(dealAvgPrice="NaN"))):
            with self.subTest(case=change):
                result = audit(entry_order=valid_entry, reviewed_stop_only_request=True, **change)
                self.assertFalse(result.stop_loss_economic_behavior_consistent)
                self.assertFalse(result.stop_loss_trigger_proven)
        for entry in (dict(valid_entry, positionId="0"), dict(valid_entry, state=2),
                      dict(valid_entry, side=1), dict(valid_entry, dealVol="0.5"),
                      dict(valid_entry, dealAvgPrice="NaN")):
            with self.subTest(entry=entry):
                result = audit(entry_order=entry, reviewed_stop_only_request=True,
                               c=close(dealAvgPrice="70100"))
                self.assertFalse(result.stop_loss_economic_behavior_consistent)
        result = audit(entry_order=valid_entry, reviewed_stop_only_request=False,
                       c=close(dealAvgPrice="70100"))
        self.assertFalse(result.stop_loss_economic_behavior_consistent)

    def test_redacted_result_does_not_expose_order_ids_or_price(self):
        result = audit()
        for forbidden in ("321", "789", "70000"):
            self.assertNotIn(forbidden, str(result))


if __name__ == "__main__":
    unittest.main()
