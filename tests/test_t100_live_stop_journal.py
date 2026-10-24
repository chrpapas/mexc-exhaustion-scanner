"""No-POST stop journal and native-stop read-only verification tests."""
import unittest
from decimal import Decimal as D
from app.t100_live_stop_journal import StopAmendment, ReadOnlyStopReconciler, StopJournalHalted


class Journal:
    def __init__(self):
        self.states = {}
        self.transitions = []
    async def prepare(self, a):
        if a.external_id in self.states:
            return False
        self.states[a.external_id] = "PREPARED"
        return True
    async def state(self, oid):
        return self.states[oid]
    async def transition(self, oid, expected, target):
        self.transitions.append((expected,target))
        if self.states[oid] != expected:
            return False
        self.states[oid] = target
        return True


class Exchange:
    def __init__(self):
        self.stops = [dict(positionId="10", id="20", stopLossPrice="86.5", state=1)]
        self.reads = 0
    async def open_stop_orders(self, symbol=None):
        self.reads += 1
        return self.stops


def amendment():
    return StopAmendment.build(
        symbol="SIREN_USDT", position_id="10", stop_order_id="20",
        previous_stop=D("175"), proposed_stop=D("86.5")
    )


class StopJournalTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.journal, self.exchange = Journal(), Exchange()
        self.a = amendment()
        self.assertTrue(await self.journal.prepare(self.a))
        self.reconciler = ReadOnlyStopReconciler(self.journal, self.exchange)

    async def test_unique_deterministic_intent(self):
        self.assertEqual(self.a.external_id, amendment().external_id)
        self.assertFalse(await self.journal.prepare(amendment()))

    async def test_verified_stop(self):
        self.assertEqual(await self.reconciler.verify(self.a), "VERIFIED_READ_ONLY")
        self.assertEqual(self.journal.states[self.a.external_id], "VERIFIED")
        self.assertEqual(self.exchange.reads, 1)

    async def test_idempotent_recheck(self):
        await self.reconciler.verify(self.a)
        await self.reconciler.verify(self.a)
        self.assertEqual(len(self.journal.transitions), 1)

    async def test_unapplied_stop_blocks(self):
        self.exchange.stops[0]["stopLossPrice"] = "175"
        with self.assertRaisesRegex(StopJournalHalted, "STOP_NOT_TIGHTENED"):
            await self.reconciler.verify(self.a)
        self.assertEqual(self.journal.states[self.a.external_id], "PREPARED")

    async def test_loosened_stop_blocks(self):
        self.exchange.stops[0]["stopLossPrice"] = "176"
        with self.assertRaisesRegex(StopJournalHalted, "SHORT_STOP_LOOSENED"):
            await self.reconciler.verify(self.a)

    async def test_missing_stop_blocks(self):
        self.exchange.stops = []
        with self.assertRaisesRegex(StopJournalHalted, "NOT_UNIQUELY_FOUND"):
            await self.reconciler.verify(self.a)

    async def test_duplicate_stop_blocks(self):
        self.exchange.stops.append(dict(self.exchange.stops[0]))
        with self.assertRaisesRegex(StopJournalHalted, "NOT_UNIQUELY_FOUND"):
            await self.reconciler.verify(self.a)

    async def test_inactive_stop_blocks(self):
        self.exchange.stops[0]["state"] = 2
        with self.assertRaisesRegex(StopJournalHalted, "INVALID_NATIVE_STOP"):
            await self.reconciler.verify(self.a)

    async def test_non_tightening_proposal_rejected(self):
        with self.assertRaisesRegex(ValueError, "MUST_TIGHTEN"):
            StopAmendment.build(symbol="SIREN_USDT", position_id="10", stop_order_id="20",
                                previous_stop=D("175"), proposed_stop=D("175"))

    async def test_unknown_state_still_requires_exchange_proof(self):
        self.journal.states[self.a.external_id] = "UNKNOWN"
        self.assertEqual(await self.reconciler.verify(self.a), "VERIFIED_READ_ONLY")
        self.assertEqual(self.journal.transitions, [("UNKNOWN", "VERIFIED")])
