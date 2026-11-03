"""Offline restart-safe protected-entry reconciliation tests."""
import unittest
from decimal import Decimal as D

from app.t100_live_checkpoint_coordinator import (
    CheckpointPersistenceCoordinator,
)
from app.t100_live_checkpoint_journal import CheckpointJournalHalted
from app.t100_live_entry_reconciliation import (
    ProtectedEntryReconciliationHalted,
    ProtectedEntryReconciliationPipeline,
)


class IntentReader:
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
            raise RuntimeError("unknown")
        return dict(self.row)


class Exchange:
    def __init__(self):
        self.order = {
            "orderId": "100",
            "externalOid": "t100v1eabc",
            "symbol": "SIREN_USDT",
            "side": 3,
            "state": 3,
            "positionId": "10",
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
            "fee": D("0.2"),
            "feeCurrency": "USDT",
            "timestamp": 1000,
            "category": 1,
        }]
        self.order_reads = 0
        self.fill_reads = 0

    async def order_by_external_oid(self, symbol, external_oid):
        self.order_reads += 1
        return dict(self.order) if self.order is not None else None

    async def order_deal_details(self, order_id):
        self.fill_reads += 1
        return [dict(x) for x in self.fills]


class FillStore:
    def __init__(self):
        self.digest = None
        self.calls = 0

    async def persist(self, fill_set):
        self.calls += 1
        if self.digest is None:
            self.digest = fill_set.fill_digest_sha256
            return "PERSISTED"
        if self.digest != fill_set.fill_digest_sha256:
            raise RuntimeError("immutability mismatch")
        return "ALREADY_VERIFIED"


class Writer:
    def __init__(self):
        self.current = None
        self.fail = False

    async def create_initial(self, current):
        if self.fail:
            raise CheckpointJournalHalted("db failed")
        if self.current is not None:
            if self.current != current:
                raise CheckpointJournalHalted(
                    "checkpoint immutability mismatch"
                )
            return
        self.current = current

    async def advance(self, previous, current, *, reason):
        raise AssertionError("not used")


def position(**kw):
    row = {
        "positionId": "10",
        "symbol": "SIREN_USDT",
        "positionType": 2,
        "state": 1,
        "holdVol": D("20"),
        "holdAvgPrice": D("100"),
    }
    row.update(kw)
    return row


def stop(**kw):
    row = {
        "id": "99",
        "positionId": "10",
        "symbol": "SIREN_USDT",
        "positionType": 2,
        "state": 1,
        "isFinished": 0,
        "stopLossPrice": D("175"),
        "vol": D("20"),
        "volType": 1,
        "profitLossVolType": "SAME",
    }
    row.update(kw)
    return row


def gates(**kw):
    data = {
        "evidence_fresh": True,
        "pre_entry_account_reconciled": True,
        "positions_complete": True,
        "stops_complete": True,
    }
    data.update(kw)
    return data


class ProtectedEntryTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.intents = IntentReader()
        self.exchange = Exchange()
        self.fills = FillStore()
        self.writer = Writer()
        self.pipeline = ProtectedEntryReconciliationPipeline(
            intents=self.intents,
            exchange=self.exchange,
            fills=self.fills,
            checkpoints=CheckpointPersistenceCoordinator(self.writer),
        )

    async def run(self, **kw):
        data = {
            "external_oid": "t100v1eabc",
            "positions": [position()],
            "stops": [stop()],
        }
        data.update(gates())
        data.update(kw)
        return await self.pipeline.reconcile_and_persist(**data)

    async def test_full_entry_evidence_persists_fill_and_checkpoint(self):
        r = await self.run()
        self.assertEqual(r.checkpoint.exchange_order_id, "100")
        self.assertEqual(r.checkpoint.position_id, "10")
        self.assertEqual(r.checkpoint.stop_order_id, "99")
        self.assertEqual(r.fill_set.order_id, "100")
        self.assertEqual(r.fill_persistence_status, "PERSISTED")
        self.assertFalse(r.live_authorization)

    async def test_unacked_or_mismatched_durable_intent_blocks(self):
        for field, value in (
            ("state", "UNKNOWN"),
            ("exchange_order_id", None),
            ("external_oid", "other"),
        ):
            original = self.intents.row[field]
            self.intents.row[field] = value
            with self.subTest(field=field):
                with self.assertRaises(
                    ProtectedEntryReconciliationHalted
                ):
                    await self.run()
            self.intents.row[field] = original
        self.assertEqual(self.fills.calls, 0)

    async def test_missing_exchange_order_never_resubmits(self):
        self.exchange.order = None
        with self.assertRaisesRegex(
            ProtectedEntryReconciliationHalted,
            "NO_RESUBMIT",
        ):
            await self.run()
        self.assertEqual(self.fills.calls, 0)

    async def test_order_identity_or_partial_fill_blocks(self):
        cases = (
            ("orderId", "101"),
            ("externalOid", "other"),
            ("symbol", "OTHER_USDT"),
            ("state", 2),
            ("dealVol", D("19")),
        )
        for field, value in cases:
            original = self.exchange.order[field]
            self.exchange.order[field] = value
            with self.subTest(field=field):
                with self.assertRaises(
                    ProtectedEntryReconciliationHalted
                ):
                    await self.run()
            self.exchange.order[field] = original
        self.assertEqual(self.fills.calls, 0)

    async def test_fill_mismatch_blocks_before_checkpoint(self):
        self.exchange.fills[0]["vol"] = D("19")
        with self.assertRaisesRegex(
            ProtectedEntryReconciliationHalted,
            "FILL_RECONCILIATION_BLOCKED",
        ):
            await self.run()
        self.assertIsNone(self.writer.current)

    async def test_unprotected_inventory_blocks_before_persistence(self):
        with self.assertRaisesRegex(
            ProtectedEntryReconciliationHalted,
            "PROTECTION_SEMANTICS_UNVERIFIED",
        ):
            await self.run(stops=[])
        self.assertEqual(self.fills.calls, 0)
        self.assertIsNone(self.writer.current)

    async def test_whole_position_same_native_stop_is_accepted(self):
        r = await self.run(stops=[stop(
            vol=D("0"),
            volType=2,
            profitLossVolType="SAME",
        )])
        self.assertEqual(r.checkpoint.stop_order_id, "99")

    async def test_pre_entry_reconciliation_gate_is_required(self):
        with self.assertRaisesRegex(
            ProtectedEntryReconciliationHalted,
            "PRE_ENTRY_ACCOUNT_NOT_RECONCILED",
        ):
            await self.run(pre_entry_account_reconciled=False)
        self.assertEqual(self.fills.calls, 1)
        self.assertIsNone(self.writer.current)

    async def test_checkpoint_failure_after_fill_persistence_is_restart_safe(self):
        self.writer.fail = True
        with self.assertRaisesRegex(
            ProtectedEntryReconciliationHalted,
            "CHECKPOINT_PERSISTENCE_BLOCKED",
        ):
            await self.run()
        self.assertEqual(self.fills.calls, 1)
        self.assertIsNotNone(self.fills.digest)
        self.assertIsNone(self.writer.current)

        self.writer.fail = False
        recovered = await self.run()
        self.assertEqual(
            recovered.fill_persistence_status,
            "ALREADY_VERIFIED",
        )
        self.assertEqual(recovered.checkpoint.position_id, "10")
        self.assertEqual(self.fills.calls, 2)

    async def test_incomplete_post_entry_inventory_blocks_checkpoint(self):
        for field in ("positions_complete", "stops_complete"):
            with self.subTest(field=field):
                self.fills = FillStore()
                self.writer = Writer()
                self.pipeline = ProtectedEntryReconciliationPipeline(
                    intents=self.intents,
                    exchange=self.exchange,
                    fills=self.fills,
                    checkpoints=CheckpointPersistenceCoordinator(
                        self.writer
                    ),
                )
                with self.assertRaisesRegex(
                    ProtectedEntryReconciliationHalted,
                    "INCOMPLETE",
                ):
                    await self.run(**{field: False})
                self.assertEqual(self.fills.calls, 1)
                self.assertIsNone(self.writer.current)


if __name__ == "__main__":
    unittest.main()
