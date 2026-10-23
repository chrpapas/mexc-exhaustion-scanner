"""Offline lifecycle audit tests. Zero HTTP POSTs, no credentials."""
from decimal import Decimal as D
import unittest

from app.t100_live_lifecycle_audit import LifecycleAuditor, LifecycleHalted, TrackedOrder


class FakeExchange:
    def __init__(self):
        self.order = dict(symbol="SIREN_USDT", externalOid="t100v1eabc",
                          orderId="101", side=3, vol="20", dealVol="20",
                          dealAvgPrice="2", state=3, positionId="22")
        self.positions = [dict(symbol="SIREN_USDT", positionId="22",
                               holdVol="20", positionType=2)]
        self.stops = [dict(positionId="22", id="1001", state=1,
                           stopLossPrice="3.5", vol="20")]
        self.reads = 0
    async def order_by_external_oid(self, symbol, external_oid):
        self.reads += 1
        return self.order
    async def open_positions(self, symbol=None):
        self.reads += 1
        return self.positions
    async def open_stop_orders(self, symbol=None):
        self.reads += 1
        return self.stops


class AuditTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.exchange = FakeExchange()
        self.audit = LifecycleAuditor(self.exchange)
        self.tracked = TrackedOrder("SIREN_USDT", "t100v1eabc", "101", D("20"), "22")

    async def test_filled_and_protected(self):
        r = await self.audit.inspect(self.tracked)
        self.assertEqual(r.status, "FILLED_PROTECTED_NEEDS_FEE_FUNDING_RECONCILIATION")
        self.assertEqual(r.filled_contracts, D("20"))
        self.assertEqual(self.exchange.reads, 3)

    async def test_partial_protected_blocks_new_entry(self):
        self.exchange.order["dealVol"] = "8"
        self.exchange.positions[0]["holdVol"] = "8"
        self.exchange.stops[0]["vol"] = "8"
        r = await self.audit.inspect(self.tracked)
        self.assertEqual(r.status, "PARTIAL_PROTECTED_BLOCK_NEW_ENTRY")
        self.assertEqual(r.unfilled_contracts, D("12"))

    async def test_unfilled_blocks_new_entry(self):
        self.exchange.order.update(dealVol="0", dealAvgPrice=None, positionId=None, state=2)
        self.exchange.positions = []
        self.exchange.stops = []
        tracked = TrackedOrder("SIREN_USDT", "t100v1eabc", "101", D("20"))
        self.assertEqual((await self.audit.inspect(tracked)).status, "ACK_UNFILLED_BLOCK_NEW_ENTRY")

    async def test_unprotected_partial_fails(self):
        self.exchange.order["dealVol"] = "8"
        self.exchange.positions[0]["holdVol"] = "8"
        self.exchange.stops = []
        with self.assertRaisesRegex(LifecycleHalted, "NOT_UNIQUELY_PROTECTED"):
            await self.audit.inspect(self.tracked)

    async def test_wrong_order_identity_fails(self):
        self.exchange.order["externalOid"] = "other"
        with self.assertRaisesRegex(LifecycleHalted, "IDENTITY"):
            await self.audit.inspect(self.tracked)

    async def test_overfill_fails(self):
        self.exchange.order["dealVol"] = "21"
        with self.assertRaisesRegex(LifecycleHalted, "OVERFILL"):
            await self.audit.inspect(self.tracked)

    async def test_missing_price_fails(self):
        self.exchange.order["dealAvgPrice"] = None
        with self.assertRaisesRegex(LifecycleHalted, "NUMERIC"):
            await self.audit.inspect(self.tracked)

    async def test_unknown_exchange_state_fails(self):
        self.exchange.order["state"] = 999
        with self.assertRaisesRegex(LifecycleHalted, "UNKNOWN_EXCHANGE"):
            await self.audit.inspect(self.tracked)

    async def test_missing_account_response_fails(self):
        self.exchange.positions = None
        with self.assertRaisesRegex(LifecycleHalted, "UNVERIFIABLE"):
            await self.audit.inspect(self.tracked)

    async def test_duplicate_native_stops_fails(self):
        self.exchange.stops.append(dict(self.exchange.stops[0]))
        with self.assertRaisesRegex(LifecycleHalted, "NOT_UNIQUELY_PROTECTED"):
            await self.audit.inspect(self.tracked)

    async def test_stop_less_than_position_fails(self):
        self.exchange.stops[0]["vol"] = "19"
        with self.assertRaisesRegex(LifecycleHalted, "NOT_UNIQUELY_PROTECTED"):
            await self.audit.inspect(self.tracked)

    async def test_account_side_drift_fails(self):
        self.exchange.positions[0]["positionType"] = 1
        with self.assertRaisesRegex(LifecycleHalted, "SIDE_DRIFT"):
            await self.audit.inspect(self.tracked)
