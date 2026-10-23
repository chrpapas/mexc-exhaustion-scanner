"""Offline supervisor checks. No credentials or live requests."""
from decimal import Decimal as D
import unittest
from app.t100_live_supervisor import LivePositionSupervisor, ManagedShort, SupervisionHalted

class FakeExchange:
    def __init__(self):
        self.positions=[]
        self.stops=[]
    async def open_positions(self,symbol=None):
        return self.positions
    async def open_stop_orders(self,symbol=None):
        return self.stops

def position():
    return ManagedShort("SIREN_USDT","10",D("20"),D("100"),"99",D("175"),D("0"),False)

class SupervisorTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.exchange=FakeExchange()
        self.s=LivePositionSupervisor(self.exchange)
        self.exchange.positions=[dict(symbol="SIREN_USDT",positionId="10",holdVol="20",positionType=2)]
        self.exchange.stops=[dict(positionId="10",id="99",state=1,stopLossPrice="175",vol="20")]
    async def test_healthy(self):
        await self.s.verify([position()])
    async def test_unknown_position(self):
        self.exchange.positions.append(dict(symbol="UNKNOWN_USDT",positionId="11",holdVol="1",positionType=2))
        with self.assertRaisesRegex(SupervisionHalted,"UNRECOGNIZED"):
            await self.s.verify([position()])
    async def test_missing_native_stop(self):
        self.exchange.stops=[]
        with self.assertRaisesRegex(SupervisionHalted,"NATIVE_STOP"):
            await self.s.verify([position()])
    async def test_insufficient_stop_contracts(self):
        self.exchange.stops[0]["vol"]="19"
        with self.assertRaisesRegex(SupervisionHalted,"COVERAGE"):
            await self.s.verify([position()])
    async def test_drifted_contracts(self):
        self.exchange.positions[0]["holdVol"]="19"
        with self.assertRaisesRegex(SupervisionHalted,"VOLUME_DRIFT"):
            await self.s.verify([position()])
    async def test_unexpected_side(self):
        self.exchange.positions[0]["positionType"]=1
        with self.assertRaisesRegex(SupervisionHalted,"SIDE_DRIFT"):
            await self.s.verify([position()])
    async def test_no_positions(self):
        self.exchange.positions=[]
        self.exchange.stops=[]
        await self.s.verify([])
    async def test_trail_proposal_only(self):
        proposed=self.s.propose(position(),observed_low=D("86"),price_tick=D("0.01"))
        self.assertEqual(proposed.proposed_stop,D("86.50"))
        self.assertEqual(proposed.old_stop,D("175"))
