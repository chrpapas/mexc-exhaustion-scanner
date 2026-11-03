"""Offline verified fill-set construction tests."""
import unittest
from app.t100_live_fill_ledger import (
    VerifiedFillLedgerHalted,
    build_verified_order_fill_set,
)

def order(**kw):
    row=dict(
        orderId="100",positionId="10",symbol="SIREN_USDT",
        side=3,state=3,dealVol=5,dealAvgPrice=102,
        feeCurrency="USDT")
    row.update(kw)
    return row

def fill(i,vol,price,**kw):
    row=dict(
        id=str(i),orderId="100",symbol="SIREN_USDT",side=3,
        vol=vol,price=price,fee=0.1,feeCurrency="USDT",
        timestamp=1000+i,category=1)
    row.update(kw)
    return row

class FillLedgerTests(unittest.TestCase):
    def test_full_open_short_builds_verified_set(self):
        x=build_verified_order_fill_set(
            order_id="100",order=order(),
            fills=[fill(1,2,100),fill(2,3,103.33333333333333)])
        self.assertEqual(x.side,"OPEN_SHORT")
        self.assertEqual(x.position_id,"10")
        self.assertEqual(len(x.fills),2)
        self.assertTrue(x.strategy_fully_filled_order_complete)

    def test_full_close_short_builds_verified_set(self):
        x=build_verified_order_fill_set(
            order_id="100",
            order=order(side=2,dealVol=5,dealAvgPrice=90),
            fills=[fill(1,5,90,side=2)])
        self.assertEqual(x.side,"CLOSE_SHORT")

    def test_canceled_partial_rejected_for_strategy(self):
        with self.assertRaisesRegex(
            VerifiedFillLedgerHalted,"NOT_FULLY_FILLED"):
            build_verified_order_fill_set(
                order_id="100",
                order=order(state=4,dealVol=2,dealAvgPrice=100),
                fills=[fill(1,2,100)])

    def test_canceled_partial_can_be_persisted_for_forensics(self):
        x=build_verified_order_fill_set(
            order_id="100",
            order=order(state=4,dealVol=2,dealAvgPrice=100),
            fills=[fill(1,2,100)],
            require_strategy_fully_filled=False)
        self.assertFalse(x.strategy_fully_filled_order_complete)

    def test_liquidation_or_adl_category_requires_review(self):
        with self.assertRaisesRegex(
            VerifiedFillLedgerHalted,"NONSTANDARD_EXECUTION_CATEGORY"):
            build_verified_order_fill_set(
                order_id="100",order=order(),
                fills=[fill(1,5,102,category=2)])

    def test_non_usdt_fee_currency_blocks(self):
        with self.assertRaisesRegex(
            VerifiedFillLedgerHalted,"STRATEGY_CONTRACT_MISMATCH"):
            build_verified_order_fill_set(
                order_id="100",order=order(feeCurrency="MX"),
                fills=[fill(1,5,102,feeCurrency="MX")])

    def test_digest_is_order_independent_for_same_fill_set(self):
        a=build_verified_order_fill_set(
            order_id="100",order=order(),
            fills=[fill(1,2,100),fill(2,3,103.33333333333333)])
        b=build_verified_order_fill_set(
            order_id="100",order=order(),
            fills=[fill(2,3,103.33333333333333),fill(1,2,100)])
        self.assertEqual(a.fill_digest_sha256,b.fill_digest_sha256)

if __name__=="__main__":
    unittest.main()
