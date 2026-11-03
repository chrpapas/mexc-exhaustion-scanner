"""Read-only restart gate checks; no live API or database access."""
import unittest
from app.t100_live_recovery import RestartRecoveryGate, RecoveryHalted


class Ledger:
    def __init__(self):
        self.rows = []
        self.stop_rows = []
        self.fail = False
    async def fetch(self, query):
        assert "t100_live_order_intents" in query or "t100_live_stop_amendments" in query
        if self.fail:
            raise ConnectionError("db down")
        return self.stop_rows if 't100_live_stop_amendments' in query else self.rows


class Exchange:
    def __init__(self):
        self.positions = []
        self.stops = []
        self.fail = False
    async def open_positions(self, symbol=None):
        if self.fail:
            raise TimeoutError()
        return self.positions
    async def open_stop_orders(self, symbol=None):
        return self.stops


class RestartTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.db, self.ex = Ledger(), Exchange()
        self.gate = RestartRecoveryGate(self.db, self.ex)

    async def test_flat_is_shadow_only(self):
        report = await self.gate.inspect()
        self.assertEqual(report.mode, "SHADOW_ONLY")
        self.assertEqual(report.persisted_intents, 0)

    async def test_prepared_intent_blocks(self):
        self.db.rows = [dict(external_oid="x", state="PREPARED", exchange_order_id=None)]
        with self.assertRaisesRegex(RecoveryHalted, "PERSISTED_INTENTS"):
            await self.gate.inspect()

    async def test_ack_intent_blocks(self):
        self.db.rows = [dict(external_oid="x", state="ACK", exchange_order_id="10")]
        with self.assertRaisesRegex(RecoveryHalted, "PERSISTED_INTENTS"):
            await self.gate.inspect()

    async def test_malformed_intent_blocks(self):
        self.db.rows = [dict(external_oid=None, state="UNKNOWN")]
        with self.assertRaisesRegex(RecoveryHalted, "LEDGER_ROWS_INVALID"):
            await self.gate.inspect()

    async def test_db_outage_blocks(self):
        self.db.fail = True
        with self.assertRaisesRegex(RecoveryHalted, "LEDGER_UNAVAILABLE"):
            await self.gate.inspect()

    async def test_unknown_position_blocks(self):
        self.ex.positions = [dict(positionId="1")]
        with self.assertRaisesRegex(RecoveryHalted, "EXCHANGE_NOT_FLAT"):
            await self.gate.inspect()

    async def test_native_stop_without_position_blocks(self):
        self.ex.stops = [dict(id="2")]
        with self.assertRaisesRegex(RecoveryHalted, "EXCHANGE_NOT_FLAT"):
            await self.gate.inspect()

    async def test_malformed_exchange_inventory_blocks(self):
        self.ex.positions = [None]
        with self.assertRaisesRegex(RecoveryHalted, "EXCHANGE_INVENTORY_MALFORMED"):
            await self.gate.inspect()

    async def test_exchange_outage_blocks(self):
        self.ex.fail = True
        with self.assertRaisesRegex(RecoveryHalted, "EXCHANGE_READ_UNAVAILABLE"):
            await self.gate.inspect()

    async def test_ambiguous_exchange_response_blocks(self):
        self.ex.positions = None
        with self.assertRaisesRegex(RecoveryHalted, "EXCHANGE_RESPONSE_AMBIGUOUS"):
            await self.gate.inspect()

    async def test_unresolved_stop_amendment_blocks(self):
        self.db.stop_rows = [dict(external_id="s1", state="UNKNOWN")]
        with self.assertRaisesRegex(RecoveryHalted, "UNRESOLVED_STOP_AMENDMENTS"):
            await self.gate.inspect()
