"""Offline final closed-position accounting tests."""
import unittest
from decimal import Decimal as D

from app.t100_live_close_provenance import VerifiedStopCloseProvenance
from app.t100_live_fill_ledger import VerifiedFill, VerifiedOrderFillSet
from app.t100_live_funding_ledger import (
    VerifiedFundingEvent, VerifiedPositionFundingSet,
)
from app.t100_live_position_accounting import (
    PositionAccountingHalted, reconcile_verified_closed_position,
)


def fill(trade,order,side,qty,price,fee):
    return VerifiedFill(
        trade_id=trade,order_id=order,position_id="10",side=side,
        contracts=D(qty),price=D(price),fee_usdt=D(fee),
        timestamp_ms=1000,category=1)

def fillset(order,side,fills,avg):
    return VerifiedOrderFillSet(
        order_id=order,position_id="10",symbol="SIREN_USDT",
        side=side,order_state=3,deal_volume=sum((x.contracts for x in fills),D("0")),
        average_price=D(avg),fee_currency="USDT",fills=tuple(fills),
        fill_digest_sha256="a"*64,
        final_order_fill_history_complete=True,
        strategy_fully_filled_order_complete=True)

def funding():
    e=VerifiedFundingEvent(
        event_id="f1",position_id="10",symbol="SIREN_USDT",
        position_type=2,position_value=D("200"),funding_cashflow_usdt=D("1"),
        rate=D("0.005"),settle_time_ms=2000)
    return VerifiedPositionFundingSet(
        position_id="10",events=(e,),total_count=1,
        funding_digest_sha256="b"*64,economics_verified=True)

def provenance():
    return VerifiedStopCloseProvenance(
        stop_order_id="99",position_id="10",symbol="SIREN_USDT",
        close_order_id="200",contracts=D("2"))

class PositionAccountingTests(unittest.TestCase):
    def test_closed_short_reconciles_from_verified_evidence(self):
        entry=fillset("100","OPEN_SHORT",[
            fill("e1","100","OPEN_SHORT","2","100","0.1")],"100")
        close=fillset("200","CLOSE_SHORT",[
            fill("c1","200","CLOSE_SHORT","2","90","0.1")],"90")
        x=reconcile_verified_closed_position(
            contract_size=D("1"),entry_fills=entry,
            close_provenance=provenance(),close_fills=close,
            funding=funding())
        self.assertEqual(x.reconciliation.realized_gross_usdt,D("20"))
        self.assertEqual(x.reconciliation.realized_net_usdt,D("20.8"))
        self.assertFalse(x.live_authorization)

    def test_close_order_must_match_stop_provenance(self):
        entry=fillset("100","OPEN_SHORT",[
            fill("e1","100","OPEN_SHORT","2","100","0.1")],"100")
        close=fillset("201","CLOSE_SHORT",[
            fill("c1","201","CLOSE_SHORT","2","90","0.1")],"90")
        with self.assertRaisesRegex(
            PositionAccountingHalted,"PROVENANCE_MISMATCH"):
            reconcile_verified_closed_position(
                contract_size=D("1"),entry_fills=entry,
                close_provenance=provenance(),close_fills=close,
                funding=funding())

    def test_partial_close_cannot_reconcile_as_closed(self):
        entry=fillset("100","OPEN_SHORT",[
            fill("e1","100","OPEN_SHORT","2","100","0.1")],"100")
        close=fillset("200","CLOSE_SHORT",[
            fill("c1","200","CLOSE_SHORT","1","90","0.1")],"90")
        with self.assertRaisesRegex(
            PositionAccountingHalted,"RECONCILIATION_FAILED"):
            reconcile_verified_closed_position(
                contract_size=D("1"),entry_fills=entry,
                close_provenance=provenance(),close_fills=close,
                funding=funding())

if __name__=="__main__":
    unittest.main()
