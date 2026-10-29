"""Offline final funding-set verification tests."""
import unittest
from app.t100_live_funding_ledger import (
    VerifiedFundingLedgerHalted,
    build_verified_closed_position_funding_set,
)
from app.t100_live_pagination import CompletePages

def row(i=1, **kw):
    data={
        "id":i,"symbol":"SIREN_USDT","positionType":2,
        "positionValue":100,"funding":0.1,"rate":0.001,
        "settleTime":1000+i,
    }
    data.update(kw)
    return data

def pages(rows):
    return CompletePages(tuple(rows),len(rows),1)

class FundingLedgerTests(unittest.TestCase):
    def test_identical_complete_collections_verify(self):
        x=build_verified_closed_position_funding_set(
            position_id="10",first=pages([row(1),row(2)]),
            second=pages([row(2),row(1)]))
        self.assertEqual(x.total_count,2)
        self.assertEqual(len(x.events),2)
        self.assertTrue(x.economics_verified)

    def test_zero_funding_history_can_be_final_and_complete(self):
        x=build_verified_closed_position_funding_set(
            position_id="10",first=pages([]),second=pages([]))
        self.assertEqual(x.total_count,0)
        self.assertTrue(x.economics_verified)

    def test_total_change_blocks(self):
        with self.assertRaisesRegex(
            VerifiedFundingLedgerHalted,"TOTAL_CHANGED"):
            build_verified_closed_position_funding_set(
                position_id="10",first=pages([row(1)]),
                second=pages([row(1),row(2)]))

    def test_value_change_blocks(self):
        with self.assertRaisesRegex(
            VerifiedFundingLedgerHalted,"ROWS_CHANGED"):
            build_verified_closed_position_funding_set(
                position_id="10",first=pages([row(1)]),
                second=pages([row(1,funding=0.2)]))

    def test_wrong_funding_semantics_blocks(self):
        with self.assertRaisesRegex(
            VerifiedFundingLedgerHalted,"ECONOMICS_UNVERIFIED"):
            build_verified_closed_position_funding_set(
                position_id="10",
                first=pages([row(1,funding=-0.1)]),
                second=pages([row(1,funding=-0.1)]))

    def test_duplicate_event_id_blocks(self):
        duplicated=CompletePages((row(1),row(1)),2,1)
        with self.assertRaisesRegex(
            VerifiedFundingLedgerHalted,"CONTRACT_INVALID"):
            build_verified_closed_position_funding_set(
                position_id="10",first=duplicated,second=duplicated)

if __name__=="__main__":
    unittest.main()
