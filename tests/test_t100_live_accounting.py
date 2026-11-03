"""Pure, deterministic accounting tests; no venue calls."""
from decimal import Decimal as D
import unittest
from app.t100_live_accounting import (
    AccountingHalted, ExecutionFill, FundingEvent, reconcile_short,
)


def fill(trade, side, qty, price, fee="0.1", pos="p1"):
    return ExecutionFill(trade, "o"+trade, pos, side, D(qty), D(price), D(fee))


def check(fills, funding=(), holding="0", **kwargs):
    return reconcile_short(
        position_id="p1", contract_size=D("2"),
        fills=fills, funding=funding,
        confirmed_exchange_holding=D(holding),
        fills_complete=kwargs.get("fills_complete", True),
        funding_complete=kwargs.get("funding_complete", True),
    )


class AccountingTests(unittest.TestCase):
    def test_profitable_short_after_fees_and_funding(self):
        r=check([
            fill("a","OPEN_SHORT","2","100"),
            fill("b","CLOSE_SHORT","2","90")
        ],[FundingEvent("f1","p1",D("1.5"))])
        self.assertEqual(r.realized_gross_usdt,D("40"))
        self.assertEqual(r.realized_net_usdt,D("41.3"))
        self.assertEqual(r.status,"CLOSED_RECONCILED")

    def test_losing_short(self):
        r=check([fill("a","OPEN_SHORT","1","100"),
                 fill("b","CLOSE_SHORT","1","120")])
        self.assertEqual(r.realized_net_usdt,D("-40.2"))

    def test_fifo_partial_close(self):
        r=check([fill("a","OPEN_SHORT","1","100"),
                 fill("b","OPEN_SHORT","2","110"),
                 fill("c","CLOSE_SHORT","2","90")],holding="1")
        self.assertEqual(r.realized_gross_usdt,D("60"))
        self.assertEqual(r.remaining_contracts,D("1"))
        self.assertEqual(r.status,"OPEN_RECONCILED_NOT_CLOSED")

    def test_negative_funding(self):
        r=check([fill("a","OPEN_SHORT","1","100"),
                 fill("b","CLOSE_SHORT","1","100")],
                [FundingEvent("f1","p1",D("-1"))])
        self.assertEqual(r.realized_net_usdt,D("-1.2"))

    def test_duplicate_trade_rejected(self):
        with self.assertRaisesRegex(AccountingHalted,"DUPLICATE_TRADE_ID"):
            check([fill("a","OPEN_SHORT","1","100"),
                   fill("a","CLOSE_SHORT","1","90")])

    def test_duplicate_funding_rejected(self):
        with self.assertRaisesRegex(AccountingHalted,"DUPLICATE_FUNDING_EVENT"):
            check([fill("a","OPEN_SHORT","1","100")],
                  [FundingEvent("f","p1",D("1")), FundingEvent("f","p1",D("1"))],
                  holding="1")

    def test_close_before_open_rejected(self):
        with self.assertRaisesRegex(AccountingHalted,"CLOSE_EXCEEDS_PRIOR_OPEN"):
            check([fill("a","CLOSE_SHORT","1","90")])

    def test_excess_close_rejected(self):
        with self.assertRaisesRegex(AccountingHalted,"CLOSE_EXCEEDS_PRIOR_OPEN"):
            check([fill("a","OPEN_SHORT","1","100"),
                   fill("b","CLOSE_SHORT","2","90")])

    def test_exchange_holding_drift_rejected(self):
        with self.assertRaisesRegex(AccountingHalted,"EXCHANGE_POSITION_QUANTITY_DRIFT"):
            check([fill("a","OPEN_SHORT","1","100")],holding="0")

    def test_missing_funding_coverage_rejected(self):
        with self.assertRaisesRegex(AccountingHalted,"INPUTS_INCOMPLETE"):
            check([fill("a","OPEN_SHORT","1","100")],
                  holding="1",funding_complete=False)

    def test_missing_fill_coverage_rejected(self):
        with self.assertRaisesRegex(AccountingHalted,"INPUTS_INCOMPLETE"):
            check([fill("a","OPEN_SHORT","1","100")],
                  holding="1",fills_complete=False)

    def test_mixed_position_rejected(self):
        with self.assertRaisesRegex(AccountingHalted,"POSITION_ID_MISMATCH"):
            check([fill("a","OPEN_SHORT","1","100",pos="p2")])

    def test_nonfinite_price_rejected(self):
        with self.assertRaisesRegex(AccountingHalted,"INVALID_ACCOUNTING_NUMBER"):
            check([fill("a","OPEN_SHORT","1","NaN")])

    def test_wrong_side_rejected(self):
        with self.assertRaisesRegex(AccountingHalted,"UNSUPPORTED_FILL_SIDE"):
            check([fill("a","BUY","1","100")])

    def test_empty_fills_rejected(self):
        with self.assertRaisesRegex(AccountingHalted,"NO_CONFIRMED_FILLS"):
            check([])

    def test_zero_fee_allowed(self):
        r=check([fill("a","OPEN_SHORT","1","100",fee="0")],holding="1")
        self.assertEqual(r.fees_usdt,D("0"))
