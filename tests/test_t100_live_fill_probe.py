"""Offline tests for order-specific MEXC fill reconciliation."""
import unittest

from app.t100_live_fill_probe import assess_order_deal_reconciliation


def order(**extra):
    row = {
        "orderId": "100",
        "symbol": "SIREN_USDT",
        "side": 3,
        "state": 3,
        "dealVol": 5,
        "dealAvgPrice": 100,
        "feeCurrency": "USDT",
    }
    row.update(extra)
    return row


def fill(i, vol, price, **extra):
    row = {
        "id": str(i),
        "orderId": "100",
        "symbol": "SIREN_USDT",
        "side": 3,
        "vol": vol,
        "price": price,
        "fee": 0.1,
        "feeCurrency": "USDT",
    }
    row.update(extra)
    return row


class FillProbeTests(unittest.TestCase):
    def test_multi_fill_order_reconciles(self):
        r = assess_order_deal_reconciliation(
            "100", order(dealAvgPrice=102),
            [fill(1, 2, 100), fill(2, 3, 103.33333333333333)],
        )
        self.assertTrue(r["fill_volume_matches_order_deal_volume"])
        self.assertTrue(r["order_specific_fill_reconciliation_verified"])

    def test_order_id_mismatch_blocks(self):
        r = assess_order_deal_reconciliation(
            "100", order(orderId="101"), [fill(1, 5, 100)])
        self.assertEqual(r["status"], "ORDER_CONTRACT_MISMATCH")

    def test_fill_order_id_mismatch_detected(self):
        r = assess_order_deal_reconciliation(
            "100", order(), [fill(1, 5, 100, orderId="999")])
        self.assertFalse(r["all_fill_order_ids_match"])
        self.assertFalse(r["order_specific_fill_reconciliation_verified"])

    def test_symbol_mismatch_detected(self):
        r = assess_order_deal_reconciliation(
            "100", order(), [fill(1, 5, 100, symbol="OTHER_USDT")])
        self.assertFalse(r["all_fill_symbols_match"])

    def test_side_mismatch_detected(self):
        r = assess_order_deal_reconciliation(
            "100", order(), [fill(1, 5, 100, side=1)])
        self.assertFalse(r["all_fill_sides_match"])

    def test_duplicate_fill_id_blocks(self):
        r = assess_order_deal_reconciliation(
            "100", order(), [fill(1, 2, 100), fill(1, 3, 100)])
        self.assertEqual(r["status"], "MALFORMED_OR_DUPLICATE_FILL")

    def test_volume_mismatch_detected(self):
        r = assess_order_deal_reconciliation(
            "100", order(), [fill(1, 4, 100)])
        self.assertFalse(r["fill_volume_matches_order_deal_volume"])

    def test_average_price_mismatch_detected(self):
        r = assess_order_deal_reconciliation(
            "100", order(dealAvgPrice=101), [fill(1, 5, 100)])
        self.assertFalse(r["weighted_fill_price_matches_order_average"])

    def test_fee_currency_mismatch_detected(self):
        r = assess_order_deal_reconciliation(
            "100", order(), [fill(1, 5, 100, feeCurrency="MX")])
        self.assertFalse(r["fee_currency_consistent"])

    def test_negative_fee_is_valid_signed_expense(self):
        r = assess_order_deal_reconciliation(
            "100", order(), [fill(1, 5, 100, fee=-0.01)])
        self.assertTrue(r["fee_semantics_signed_expense_supported"])
        self.assertTrue(r["order_specific_fill_reconciliation_verified"])

    def test_zero_fill_order_with_empty_details_reconciles(self):
        r = assess_order_deal_reconciliation(
            "100", order(state=2, dealVol=0, dealAvgPrice=0), [])
        self.assertTrue(r["order_specific_fill_reconciliation_verified"])
        self.assertEqual(r["status"], "ZERO_FILL_ORDER_RECONCILED")

    def test_zero_dealvol_with_fill_rows_blocks(self):
        r = assess_order_deal_reconciliation(
            "100", order(state=2, dealVol=0, dealAvgPrice=0),
            [fill(1, 1, 100)],
        )
        self.assertEqual(r["status"], "ZERO_DEALVOL_WITH_FILL_ROWS")

    def test_dealvol_without_rows_blocks(self):
        r = assess_order_deal_reconciliation("100", order(), [])
        self.assertEqual(r["status"], "DEALVOL_WITHOUT_FILL_ROWS")

    def test_malformed_fill_blocks(self):
        r = assess_order_deal_reconciliation(
            "100", order(), [fill(1, 5, 0)])
        self.assertEqual(r["status"], "MALFORMED_OR_DUPLICATE_FILL")

    def test_no_values_leak(self):
        r = assess_order_deal_reconciliation(
            "100",
            order(symbol="SECRET_USDT", dealAvgPrice=9876.54),
            [fill(1, 5, 9876.54, symbol="SECRET_USDT", fee=123.45)],
        )
        text = str(r)
        for secret in ("SECRET_USDT", "9876.54", "123.45", "100"):
            self.assertNotIn(secret, text)
