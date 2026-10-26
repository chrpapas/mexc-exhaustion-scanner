"""Offline protected-entry recovery pipeline tests."""
import unittest
from decimal import Decimal as D

from app.t100_live_checkpoint_coordinator import (
    CheckpointPersistenceCoordinator,
)
from app.t100_live_entry_recovery import (
    EntryRecoveryHalted,
    ProtectedEntryRecoveryPipeline,
)


class IntentStore:
    def __init__(self):
        self.row = {
            "signal_id": 1,
            "symbol": "SIREN_USDT",
            "external_oid": "t100v1eabc",
            "reference_price": D("100"),
            "notional_usdt": D("2000"),
            "volume_contracts": D("20"),
            "stop_price": D("175"),
            "state": "ACK",
            "exchange_order_id": "100",
        }
    async def record(self, oid):
        if oid != self.row["external_oid"]:
            raise RuntimeError("missing")
        return dict(self.row)


class Exchange:
    def __init__(self):
        self.order = {
            "orderId": "100",
            "externalOid": "t100v1eabc",
            "positionId": "10",
            "symbol": "SIREN_USDT",
            "side": 3,
            "state": 3,
            "dealVol": D("20"),
            "dealAvgPrice": D("100"),
            "feeCurrency": "USDT",
        }
        self.fills = [{
            "id": "f1",
            "orderId": "100",
            "symbol": "SIREN_USDT",
            "side": 3,
            "vol": D("20"),
            "price": D("100"),
            "fee": D("-0.1"),
            "feeCurrency": "USDT",
            "timestamp": 1000,
            "category": 1,
        }]
        self.order_reads = 0
        self.fill_reads = 0
    async def order_by_external_oid(self, symbol, oid):
        self.order_reads += 1
        return None if self.order is None else dict(self.order)
    async def order_deal_details(self, order_id):
        self.fill_reads += 1
        return [dict(x) for x in self.fills]


class FillStore:
    def __init__(self):
        self.calls = []
    async def persist(self, fill_set):
        self.calls.append(fill_set)
        return "PERSISTED" if len(self.calls) == 1 else "ALREADY_VERIFIED"


class Writer:
    def __init__(self):
        self.calls = []
        self.fail = False
    async def create_initial(self, current):
        if self.fail:
            from app.t100_live_checkpoint_journal import CheckpointJournalHalted
            raise CheckpointJournalHalted("simulated")
        self.calls.append(current)
    async def advance(self, previous, current, *, reason):
        raise AssertionError("not used")


def position():
    return {
        "positionId": "10",
        "symbol": "SIREN_USDT",
        "positionType": 2,
        "state": 1,
        "holdVol": D("20"),
        "holdAvgPrice": D("100"),
    }


def stop():
    return {
        "id": "99",
        "positionId": "10",
        "symbol": "SIREN_USDT",
        "positionType": 2,
        "state": 1,
        "isFinished": 0,
        "stopLossPrice": D("175"),
        "vol": D("0"),
        "volType": 2,
        "profitLossVolType": "SAME",
    }


class EntryRecoveryTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.intent_store = IntentStore()
        self.exchange = Exchange()
        self.fill_store = FillStore()
        self.writer = Writer()
        self.pipeline = ProtectedEntryRecoveryPipeline(
            intent_store=self.intent_store,
            exchange=self.exchange,
            fill_store=self.fill_store,
            checkpoint_coordinator=CheckpointPersistenceCoordinator(
                self.writer
            ),
        )

    async def run(self, **kw):
        args = dict(
            external_oid="t100v1eabc",
            positions=[position()],
            stops=[stop()],
            pre_entry_account_reconciled=True,
            evidence_fresh=True,
            positions_complete=True,
            stops_complete=True,
        )
        args.update(kw)
        return await self.pipeline.reconcile_and_persist(**args)

    async def test_verified_protected_entry_persists_fill_and_checkpoint(self):
        r = await self.run()
        self.assertEqual(r.checkpoint.position_id, "10")
        self.assertEqual(r.checkpoint.exchange_order_id, "100")
        self.assertEqual(r.checkpoint.stop_order_id, "99")
        self.assertEqual(r.fill_persistence_status, "PERSISTED")
        self.assertFalse(r.live_authorization)
        self.assertEqual(len(self.fill_store.calls), 1)
        self.assertEqual(len(self.writer.calls), 1)

    async def test_unacked_intent_never_progresses(self):
        self.intent_store.row["state"] = "UNKNOWN"
        with self.assertRaisesRegex(
            EntryRecoveryHalted, "NOT_DURABLY_ACKED"
        ):
            await self.run()
        self.assertEqual(self.exchange.order_reads, 0)

    async def test_missing_order_is_never_retry_permission(self):
        self.exchange.order = None
        with self.assertRaisesRegex(
            EntryRecoveryHalted, "NO_RESUBMIT"
        ):
            await self.run()
        self.assertEqual(len(self.fill_store.calls), 0)
        self.assertEqual(len(self.writer.calls), 0)

    async def test_partial_or_nonfinal_fill_blocks(self):
        self.exchange.order["state"] = 2
        self.exchange.order["dealVol"] = D("10")
        self.exchange.order["dealAvgPrice"] = D("100")
        self.exchange.fills[0]["vol"] = D("10")
        with self.assertRaisesRegex(
            EntryRecoveryHalted, "FILL_RECONCILIATION_BLOCKED"
        ):
            await self.run()

    async def test_unprotected_position_blocks_before_persistence(self):
        bad = stop()
        bad["state"] = 2
        with self.assertRaisesRegex(
            EntryRecoveryHalted, "NATIVE_PROTECTION_UNVERIFIED"
        ):
            await self.run(stops=[bad])
        self.assertEqual(len(self.fill_store.calls), 0)

    async def test_post_entry_extra_inventory_blocks_pilot(self):
        with self.assertRaisesRegex(
            EntryRecoveryHalted, "NOT_SINGLE_PROTECTED_POSITION"
        ):
            await self.run(positions=[position(), position()])
        self.assertEqual(len(self.fill_store.calls), 0)

    async def test_checkpoint_failure_is_restart_safe_no_resubmit(self):
        self.writer.fail = True
        with self.assertRaisesRegex(
            EntryRecoveryHalted, "CHECKPOINT_PERSISTENCE_FAILED"
        ):
            await self.run()
        self.assertEqual(len(self.fill_store.calls), 1)
        self.assertEqual(self.exchange.order_reads, 1)

        self.writer.fail = False
        r = await self.run()
        self.assertEqual(r.fill_persistence_status, "ALREADY_VERIFIED")
        self.assertEqual(len(self.writer.calls), 1)
        self.assertEqual(self.exchange.order_reads, 2)

    async def test_pre_entry_reconciliation_required(self):
        with self.assertRaisesRegex(
            EntryRecoveryHalted, "PRE_ENTRY_ACCOUNT_NOT_RECONCILED"
        ):
            await self.run(pre_entry_account_reconciled=False)
        self.assertEqual(self.exchange.order_reads, 0)


if __name__ == "__main__":
    unittest.main()
