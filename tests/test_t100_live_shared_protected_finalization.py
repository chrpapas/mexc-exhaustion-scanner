"""Offline shared-account protected-fill -> checkpoint preparation.

No MEXC GET/POST, DB writes or live permit. End-to-end synthetic GET evidence
must be reconciled exactly to order-scoped deals before even preparing an ACK.
"""
from __future__ import annotations

import copy
import unittest
from decimal import Decimal as D

from app.t100_live_shared_protected_finalization import (
    prepare_shared_protected_entry_offline,
)
from app.t100_live_v40_protected_finalization import (
    PreparedProtectedBatch, ProtectedFinalizeHalted,
)
from tests.test_t100_live_shared_protected_entry_observer import (
    SharedProtectedEntryTests,
)


def deal(**updates):
    row = {
        "id": "720", "orderId": "900",
        "symbol": "NEW_USDT", "side": 3,
        "vol": D("250"), "price": D("2"),
        "fee": D("0.1"), "feeCurrency": "USDT",
        "timestamp": 1761208401000, "category": 1,
    }
    row.update(updates)
    return row


class SharedProtectedFinalizationTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.source = SharedProtectedEntryTests()
        await self.source.asyncSetUp()
        self.order = dict(self.source.order, feeCurrency="USDT")
        self.deals = [deal()]

    def prepare(self):
        src = self.source
        return prepare_shared_protected_entry_offline(
            pre_witness=src.pre_witness,
            pre_positions=src.pre_positions, pre_stops=src.pre_stops,
            stage=src.stage, intent=src.intent,
            post_positions=src.post_positions, post_stops=src.post_stops,
            post_order=self.order, post_inventory=src.post_report,
            order_deals=self.deals,
        )

    async def test_one_protected_fill_prepares_exact_immutable_provenance(self):
        result = self.prepare()
        self.assertIs(type(result), PreparedProtectedBatch)
        self.assertEqual(len(result.entries), 1)
        entry = result.entries[0]
        self.assertEqual(entry.order_id, "900")
        self.assertEqual(entry.position_id, "390")
        self.assertEqual(entry.checkpoint.symbol, "NEW_USDT")
        self.assertEqual(entry.checkpoint.entry_price, D("2"))
        self.assertEqual(entry.checkpoint.stop_price, D("3.5"))
        self.assertEqual(entry.checkpoint.contracts, D("250"))
        self.assertEqual(entry.checkpoint.checkpoint_version, 1)
        self.assertFalse(entry.checkpoint.armed)
        self.assertEqual(entry.fill_set.deal_volume, D("250"))
        self.assertTrue(entry.fill_set.strategy_fully_filled_order_complete)
        self.assertEqual(len(entry.fill_set.fills), 1)
        self.assertEqual(entry.fill_set.side, "OPEN_SHORT")
        self.assertFalse(result.durable_ack_persisted)
        self.assertFalse(result.durable_checkpoint_persisted)
        self.assertFalse(result.durable_fill_set_persisted)
        self.assertFalse(result.native_stop_exchange_atomicity_verified)
        self.assertFalse(result.order_post_attempted)
        self.assertFalse(result.live_authorization)
        self.assertFalse(result.mexc_orders_sent)

    async def test_partial_fill_or_missing_native_stop_never_prepare_ack(self):
        self.order["dealVol"] = D("249")
        with self.assertRaises(ProtectedFinalizeHalted):
            self.prepare()
        self.order["dealVol"] = D("250")
        self.source.post_stops.clear()
        with self.assertRaises(ProtectedFinalizeHalted):
            self.prepare()

    async def test_unverified_or_missing_trade_details_blocks(self):
        for modified in (
            [],
            [deal(vol=D("249"))],
            [deal(feeCurrency="MX")],
            [deal(side=2)],
            [deal(orderId="901")],
            [deal(category=2)],
            [deal(id="720"), deal(id="720")],
        ):
            self.deals = modified
            with self.subTest(deals=modified), self.assertRaises(ProtectedFinalizeHalted):
                self.prepare()

    async def test_manual_position_changed_or_stale_snapshot_blocks(self):
        self.source.post_positions[0]["holdVol"] = D("21")
        with self.assertRaises(ProtectedFinalizeHalted):
            self.prepare()
        self.source.post_positions[0]["holdVol"] = D("20")
        self.source.post_report["account_wide_completeness_verified"] = False
        with self.assertRaises(ProtectedFinalizeHalted):
            self.prepare()

    async def test_order_not_fully_final_or_wrong_side_blocks(self):
        for key, value in (
            ("state", 2),
            ("side", 1),
            ("externalOid", "tampered"),
            ("positionId", "999"),
        ):
            with self.subTest(key=key):
                self.order = dict(self.source.order, feeCurrency="USDT")
                self.order[key] = value
                with self.assertRaises(ProtectedFinalizeHalted):
                    self.prepare()

    async def test_no_write_transport_or_liveness_claim(self):
        import inspect
        import app.t100_live_shared_protected_finalization as m
        source = inspect.getsource(m)
        for forbidden in (
            "submit_short_with_stop(", "._private_post(",
            "asyncpg.", "MexcTradeClient(", ".send_one(",
        ):
            self.assertNotIn(forbidden, source)
        result = self.prepare()
        self.assertFalse(result.entry_allowed)
        self.assertFalse(result.live_authorization)


if __name__ == "__main__":
    unittest.main()
