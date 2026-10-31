"""No-order tests: four-slot protected attempt -> exact fill ledger/checkpoint."""
from __future__ import annotations

import copy
import unittest
from dataclasses import replace
from decimal import Decimal as D

from app.t100_live_v40_protected_finalization import (
    PreparedProtectedBatch, ProtectedFinalizeHalted, prepare_protected_entries,
)
from tests.test_t100_live_four_slot_attempt_outcome import ProtectedOutcomeTests


def deal(**updates):
    row = {
        "id": "2001", "orderId": "990", "symbol": "FOURTH_USDT",
        "side": 3, "vol": D("250"), "price": D("2"),
        "fee": D("0.2"), "feeCurrency": "USDT",
        "timestamp": 1761208401000, "category": 1,
    }
    row.update(updates)
    return row


class PrepareProtectedEntriesTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        source = ProtectedOutcomeTests()
        await source.asyncSetUp()
        self.stage = source.stage
        self.intent = source.intent
        self.before = source.before
        self.venue = source.venue
        self.inventory = copy.deepcopy(source.complete.result)
        self.orders = [self.venue.orders[("FOURTH_USDT", self.intent.external_oid)]]
        self.orders[0]["feeCurrency"] = "USDT"
        self.positions = copy.deepcopy(self.venue.first_positions)
        self.stops = copy.deepcopy(self.venue.first_stops)
        self.deals = {"990": [deal()]}

    def prepare(self):
        return prepare_protected_entries(
            stages=[self.stage], intents=[self.intent],
            pre_attempt_witness=self.before,
            orders=self.orders, positions=self.positions,
            stops=self.stops, inventory=self.inventory,
            deals_by_order_id=self.deals,
        )

    async def test_full_four_slot_protected_entry_prepares_exact_immutable_chain(self):
        batch = self.prepare()
        self.assertIs(type(batch), PreparedProtectedBatch)
        self.assertEqual(len(batch.entries), 1)
        x = batch.entries[0]
        self.assertEqual(x.order_id, "990")
        self.assertEqual(x.position_id, "900")
        self.assertEqual(x.checkpoint.symbol, "FOURTH_USDT")
        self.assertEqual(x.checkpoint.lifecycle_state, "OPEN")
        self.assertEqual(x.checkpoint.checkpoint_version, 1)
        self.assertEqual(x.checkpoint.stop_price, D("3.5"))
        self.assertEqual(x.checkpoint.contracts, D("250"))
        self.assertEqual(x.fill_set.deal_volume, D("250"))
        self.assertEqual(x.fill_set.average_price, D("2"))
        self.assertEqual(x.fill_set.side, "OPEN_SHORT")
        self.assertTrue(x.fill_set.strategy_fully_filled_order_complete)
        self.assertEqual(len(x.fill_set.fills), 1)
        self.assertFalse(batch.durable_ack_persisted)
        self.assertFalse(batch.durable_checkpoint_persisted)
        self.assertFalse(batch.durable_fill_set_persisted)
        self.assertFalse(batch.native_stop_exchange_atomicity_verified)
        self.assertFalse(batch.live_authorization)
        self.assertFalse(batch.mexc_orders_sent)

    async def test_partial_deal_histories_halt_with_no_checkpoint(self):
        self.deals["990"][0]["vol"] = D("249")
        with self.assertRaises(ProtectedFinalizeHalted):
            self.prepare()

    async def test_duplicate_trade_ids_halt(self):
        self.deals["990"].append(deal())
        with self.assertRaises(ProtectedFinalizeHalted):
            self.prepare()

    async def test_fills_fee_currency_and_liquidation_category_halt(self):
        for key, value in (("feeCurrency", "MX"), ("category", 2)):
            self.deals = {"990": [deal(**{key: value})]}
            with self.subTest(key=key), self.assertRaises(ProtectedFinalizeHalted):
                self.prepare()

    async def test_wrong_order_id_and_missing_deal_history_halt(self):
        self.deals = {"991": [deal()]}
        with self.assertRaisesRegex(ProtectedFinalizeHalted, "FILL_ORDER_SET_NOT_EXACT"):
            self.prepare()
        self.deals = {}
        with self.assertRaises(ProtectedFinalizeHalted):
            self.prepare()

    async def test_nonfinal_order_or_unprotected_stop_blocks(self):
        self.orders[0]["state"] = 2
        with self.assertRaises(ProtectedFinalizeHalted):
            self.prepare()
        self.orders[0]["state"] = 3
        self.stops[-1]["vol"] = D("100")
        with self.assertRaises(ProtectedFinalizeHalted):
            self.prepare()

    async def test_no_full_account_inventory_and_manual_position_halt(self):
        self.inventory["account_wide_completeness_verified"] = False
        with self.assertRaises(ProtectedFinalizeHalted):
            self.prepare()
        self.inventory["account_wide_completeness_verified"] = True
        self.positions[0]["symbol"] = "MANUAL_USDT"
        with self.assertRaises(ProtectedFinalizeHalted):
            self.prepare()

    async def test_duplicate_signal_or_relabelled_oid_cannot_pass(self):
        with self.assertRaises(ProtectedFinalizeHalted):
            prepare_protected_entries(
                stages=[self.stage, self.stage],
                intents=[self.intent, self.intent],
                pre_attempt_witness=self.before,
                orders=self.orders, positions=self.positions, stops=self.stops,
                inventory=self.inventory, deals_by_order_id=self.deals,
            )
        self.orders[0]["externalOid"] = "wrong"
        with self.assertRaises(ProtectedFinalizeHalted):
            self.prepare()

    async def test_no_transport_db_or_change_in_t100_live_authorization(self):
        import inspect
        import app.t100_live_v40_protected_finalization as module
        code = inspect.getsource(module)
        for forbidden in (
            "asyncpg.", "httpx.", "submit_short_with_stop(",
            "._private_post(", "MexcTradeClient(", "LiveMutationPermit",
        ):
            self.assertNotIn(forbidden, code)


if __name__ == "__main__":
    unittest.main()
