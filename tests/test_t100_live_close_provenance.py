"""Offline close-provenance tests."""
import unittest
from decimal import Decimal as D

from app.t100_live_close_provenance import (
    CloseProvenanceHalted,
    build_verified_stop_close_provenance,
    find_finished_stop_by_id,
)


def finished(**kw):
    row={
        "id":"99","positionId":"10","symbol":"SIREN_USDT",
        "state":3,"triggerSide":2,"positionType":2,"isFinished":1,
        "errorCode":0,"realityVol":20,"placeOrderId":"200",
    }
    row.update(kw)
    return row


class FakeProbe:
    def __init__(self,pages):
        self.pages=pages
        self.calls=[]
    async def raw_stop_page(self,page,size,*,is_finished=1):
        self.calls.append((page,size,is_finished))
        return self.pages.get(page,[])


class CloseProvenanceTests(unittest.IsolatedAsyncioTestCase):
    def test_executed_stop_links_to_close_order(self):
        p=build_verified_stop_close_provenance(
            finished_stop=finished(),
            expected_stop_order_id="99",
            expected_position_id="10",
            expected_symbol="SIREN_USDT",
            expected_contracts=D("20"))
        self.assertEqual(p.close_order_id,"200")
        self.assertEqual(p.contracts,D("20"))
        self.assertFalse(p.live_authorization)

    def test_canceled_or_failed_stop_blocks(self):
        for row in (
            finished(state=2,triggerSide=0,placeOrderId=0),
            finished(state=5,errorCode=123),
            finished(state=3,triggerSide=1),
        ):
            with self.subTest(row=row):
                with self.assertRaises(CloseProvenanceHalted):
                    build_verified_stop_close_provenance(
                        finished_stop=row,
                        expected_stop_order_id="99",
                        expected_position_id="10",
                        expected_symbol="SIREN_USDT",
                        expected_contracts=D("20"))

    def test_partial_actual_placed_volume_blocks(self):
        with self.assertRaisesRegex(
            CloseProvenanceHalted,"VOLUME_MISMATCH"):
            build_verified_stop_close_provenance(
                finished_stop=finished(realityVol=19),
                expected_stop_order_id="99",
                expected_position_id="10",
                expected_symbol="SIREN_USDT",
                expected_contracts=D("20"))

    async def test_bounded_lookup_finds_exact_id(self):
        probe=FakeProbe({
            1:[finished(id="100")],
            2:[finished(id="99")],
        })
        row=await find_finished_stop_by_id(
            probe,stop_order_id="99",page_size=100,max_pages=3)
        self.assertEqual(row["id"],"99")
        self.assertEqual(probe.calls,[(1,100,1),(2,100,1)])

    async def test_not_found_is_not_treated_as_complete_absence(self):
        probe=FakeProbe({1:[],2:[]})
        with self.assertRaisesRegex(
            CloseProvenanceHalted,"NOT_FOUND_WITHIN_BOUNDED_SEARCH"):
            await find_finished_stop_by_id(
                probe,stop_order_id="99",max_pages=2)

if __name__=="__main__":
    unittest.main()
