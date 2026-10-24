"""Offline tests for redacted account-level position economic witness."""
import unittest

from app.t100_live_position_margin_witness import (
    assess_position_margin_witness,
)


def pos(symbol="A_USDT", im="10", pnl="1"):
    return {
        "symbol": symbol,
        "im": im,
        "unRealizedPnl": pnl,
    }


def asset(currency="USDT", margin="0", unrealized="0"):
    return {
        "currency": currency,
        "positionMargin": margin,
        "unrealized": unrealized,
    }


class PositionMarginWitnessTests(unittest.TestCase):
    def test_exact_usdt_margin_and_unrealized_match(self):
        r = assess_position_margin_witness(
            [pos(im="10", pnl="1"), pos("B_USDT", im="20", pnl="-2")],
            [
                asset("USDT", margin="30", unrealized="-1"),
                asset("MX", margin="0", unrealized="0"),
            ],
        )
        self.assertTrue(r["usdt_position_margin_reconciled"])
        self.assertTrue(r["usdt_unrealized_reconciled"])
        self.assertTrue(r["returned_non_usdt_position_margin_zero"])
        self.assertTrue(r["position_inventory_economic_witness_observed"])
        self.assertFalse(r["account_wide_completeness_verified"])
        self.assertFalse(r["exchange_atomicity_verified"])
        self.assertEqual(
            r["status"], "POSITION_INVENTORY_ECONOMIC_WITNESS_MATCH")

    def test_hidden_position_margin_would_create_mismatch(self):
        r = assess_position_margin_witness(
            [pos(im="10", pnl="1")],
            [asset("USDT", margin="20", unrealized="1")],
        )
        self.assertFalse(r["usdt_position_margin_reconciled"])
        self.assertFalse(r["position_inventory_economic_witness_observed"])
        self.assertEqual(
            r["status"], "POSITION_INVENTORY_ECONOMIC_WITNESS_MISMATCH")

    def test_unrealized_mismatch_is_independent_failure(self):
        r = assess_position_margin_witness(
            [pos(im="10", pnl="1")],
            [asset("USDT", margin="10", unrealized="2")],
        )
        self.assertTrue(r["usdt_position_margin_reconciled"])
        self.assertFalse(r["usdt_unrealized_reconciled"])
        self.assertFalse(r["position_inventory_economic_witness_observed"])

    def test_non_usdt_returned_asset_position_margin_blocks(self):
        r = assess_position_margin_witness(
            [pos(im="10", pnl="1")],
            [
                asset("USDT", margin="10", unrealized="1"),
                asset("USDC", margin="5", unrealized="0"),
            ],
        )
        self.assertFalse(r["returned_non_usdt_position_margin_zero"])
        self.assertFalse(r["position_inventory_economic_witness_observed"])

    def test_non_usdt_returned_asset_unrealized_blocks(self):
        r = assess_position_margin_witness(
            [pos(im="10", pnl="1")],
            [
                asset("USDT", margin="10", unrealized="1"),
                asset("USDC", margin="0", unrealized="-0.1"),
            ],
        )
        self.assertFalse(r["returned_non_usdt_unrealized_zero"])
        self.assertFalse(r["position_inventory_economic_witness_observed"])

    def test_non_usdt_position_row_blocks_scope(self):
        r = assess_position_margin_witness(
            [pos("BTC_USD", im="10", pnl="1")],
            [asset("USDT", margin="10", unrealized="1")],
        )
        self.assertEqual(r["status"], "NON_USDT_POSITION_OBSERVED")
        self.assertFalse(r["position_inventory_economic_witness_observed"])

    def test_usdt_asset_must_be_unique(self):
        for assets in (
            [],
            [asset("USDT"), asset("USDT")],
        ):
            with self.subTest(assets=len(assets)):
                r = assess_position_margin_witness([], assets)
                self.assertEqual(
                    r["status"], "USDT_ASSET_ROW_NOT_UNIQUE")

    def test_flat_zero_margin_account_can_match(self):
        r = assess_position_margin_witness(
            [],
            [
                asset("USDT", margin="0", unrealized="0"),
                asset("MX", margin="0", unrealized="0"),
            ],
        )
        self.assertTrue(r["position_inventory_economic_witness_observed"])

    def test_small_rounding_difference_tolerated(self):
        r = assess_position_margin_witness(
            [pos(im="10", pnl="1")],
            [asset(
                "USDT",
                margin="10.000000001",
                unrealized="1.000000001",
            )],
        )
        self.assertTrue(r["position_inventory_economic_witness_observed"])

    def test_missing_or_nonfinite_numeric_field_blocks(self):
        for bad in (None, "NaN", "Infinity"):
            r = assess_position_margin_witness(
                [pos(im=bad, pnl="1")],
                [asset("USDT", margin="10", unrealized="1")],
            )
            self.assertFalse(r["position_inventory_economic_witness_observed"])
            self.assertTrue(r["status"].startswith("INVALID_"))

    def test_output_contains_no_account_values(self):
        r = assess_position_margin_witness(
            [{
                "symbol": "SECRET_USDT",
                "im": "12345.6789",
                "unRealizedPnl": "-987.654",
            }],
            [{
                "currency": "USDT",
                "positionMargin": "12345.6789",
                "unrealized": "-987.654",
            }],
        )
        text = str(r)
        for secret in ("SECRET_USDT", "12345.6789", "-987.654"):
            self.assertNotIn(secret, text)


if __name__ == "__main__":
    unittest.main()
