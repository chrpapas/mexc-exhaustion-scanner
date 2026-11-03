"""Offline four-slot STAGE -> GET -> PREPARE -> atomic-COMMIT -> restart drill.

Uses real repository policy/journal/GET evidence/DB-commit classes but entirely
fake venue and rollback-capable fake DB. No exchange POST, no key, no live
credential, no production SQL, no Render worker. A simulated exchange fill is
NOT a live fill or an order-submission certification.
"""
from __future__ import annotations

from copy import deepcopy
import unittest

from app.t100_live_durable_recovery import recover_nonflat
from app.t100_live_four_slot_protected_get_evidence import (
    observe_and_prepare_staged_protection_get_only,
)
from app.t100_live_v40_protected_commit import ProtectedCommitHalted
from tests.test_t100_live_four_slot_attempt_journal import lease
from tests.test_t100_live_four_slot_snapshot_coordinator import fixture
from tests.test_t100_live_v40_protected_commit import ProtectedCommitTests
from tests.test_t100_live_v40_protected_finalization import deal


class SimulatedFullLifecycleTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        source = ProtectedCommitTests()
        await source.asyncSetUp()
        self.source = source
        self.pg = source.pg
        self.venue = source.prior.venue
        self.intent = source.prior.intent
        self.stage = source.prior.stage
        self.witness = source.prior.before
        self.completeness = source.prior.complete
        self.venue.orders[(self.intent.symbol, self.intent.external_oid)][
            "feeCurrency"
        ] = "USDT"
        self.deal_records = [deal()]
        self.deal_reads = 0

        async def get_deals(order_id):
            self.deal_reads += 1
            if order_id != "990":
                raise AssertionError("unexpected order ID")
            return deepcopy(self.deal_records)

        self.venue.order_deal_details = get_deals

    async def prepare(self):
        return await observe_and_prepare_staged_protection_get_only(
            stages=[self.stage],
            intents=[self.intent],
            pre_attempt_witness=self.witness,
            venue=self.venue,
            completeness=self.completeness,
        )

    def database_state(self):
        return deepcopy((
            self.pg.new_intents, self.pg.reserved,
            self.pg.committed_fill_sets, self.pg.committed_fills,
            self.pg.committed_checkpoints, self.pg.committed_revisions,
        ))

    async def persist(self, batch):
        return await self.source.store.persist_offline_unclaimed(
            batch=batch, pre_attempt_witness=self.witness,
            lease=lease(),
            observed_at=self.pg.reserved[0]["snapshot_observed_at"],
        )

    async def test_stage_get_prepare_commit_restart_proves_four_owned_positions(self):
        # Stage has already been durably simulated by ProtectedCommitTests.
        self.assertEqual(self.pg.new_intents[0]["state"], "SUBMITTING")
        self.assertEqual(self.pg.reserved[0]["state"], "SUBMITTING")
        self.assertEqual(self.pg.committed_fill_sets, {})

        result = await self.prepare()
        self.assertEqual(
            result.status, "PROTECTED_GET_BATCH_PREPARED_NONAUTHORIZING"
        )
        self.assertTrue(result.two_bookends_equal)
        self.assertEqual(self.deal_reads, 2)
        self.assertFalse(result.live_authorization)
        self.assertFalse(result.mexc_orders_sent)
        self.assertEqual(self.pg.new_intents[0]["state"], "SUBMITTING")
        self.assertEqual(self.pg.committed_checkpoints, {})

        persisted = await self.persist(result.prepared_batch)
        self.assertEqual(
            persisted.status,
            "V40_PROTECTED_RECORDS_ATOMICALLY_PERSISTED_NONAUTHORIZING"
        )
        self.assertEqual(persisted.entries_committed, 1)
        self.assertFalse(persisted.live_authorization)
        self.assertFalse(persisted.mexc_orders_sent)
        self.assertEqual(self.pg.new_intents[0]["state"], "ACK")
        self.assertEqual(self.pg.reserved[0]["state"], "PROTECTED")
        self.assertEqual(len(self.pg.committed_fill_sets), 1)
        self.assertEqual(len(self.pg.committed_fills), 1)
        self.assertEqual(len(self.pg.committed_checkpoints), 1)
        self.assertEqual(len(self.pg.committed_revisions), 1)

        # Reconstruct restart ledger from actual fake-DB INSERT arguments,
        # NOT the input batch's hypothetical checkpoint. A missing write or
        # wrong inserted field must break the independent recovery witness.
        snap, old_orders, _, _ = fixture(3)
        cp_fields = (
            "position_id", "symbol", "external_oid", "exchange_order_id",
            "contracts", "entry_price", "stop_order_id", "stop_price",
            "best_profit_pct", "armed", "lifecycle_state",
            "checkpoint_version", "last_write_reason",
        )
        revision_fields = (
            "position_id", "checkpoint_version", "write_reason", "symbol",
            "external_oid", "exchange_order_id", "contracts",
            "entry_price", "stop_order_id", "stop_price",
            "best_profit_pct", "armed", "lifecycle_state",
        )
        cp_values = self.pg.committed_checkpoints["900"]
        revision_values = self.pg.committed_revisions[("900", 1)]
        self.assertEqual(len(cp_values), len(cp_fields))
        self.assertEqual(len(revision_values), len(revision_fields))
        snap.checkpoints.append(dict(zip(cp_fields, cp_values)))
        snap.revisions.append(dict(zip(revision_fields, revision_values)))
        snap.intents.append(deepcopy(self.pg.new_intents[0]))
        old_orders.append(deepcopy(
            self.venue.orders[(self.intent.symbol, self.intent.external_oid)]
        ))
        positions = deepcopy(self.venue.first_positions)
        stops = deepcopy(self.venue.first_stops)
        recovered = recover_nonflat(
            checkpoint_rows=snap.checkpoints,
            revision_rows=snap.revisions,
            intent_rows=snap.intents,
            stop_amendment_rows=snap.stop_amendments,
            orders=old_orders, positions=positions, stops=stops,
            positions_complete=True, stops_complete=True,
        )
        self.assertEqual(len(recovered.recovered), 4)
        self.assertFalse(recovered.live_authorization)
        before = self.database_state()
        with self.assertRaises(ProtectedCommitHalted):
            await self.persist(result.prepared_batch)
        self.assertEqual(self.database_state(), before)

    async def test_crash_after_stage_or_observation_does_not_ack_or_post(self):
        # Crash A: durable SUBMITTING exists, nothing observed or persisted.
        after_stage = self.database_state()
        self.assertEqual(self.pg.new_intents[0]["state"], "SUBMITTING")
        self.assertEqual(self.venue.positions_reads, 0)
        self.assertEqual(self.venue.order_reads, 0)

        # Restart performs read-only reconciliation; no second stage/POST.
        first = await self.prepare()
        self.assertEqual(
            first.status, "PROTECTED_GET_BATCH_PREPARED_NONAUTHORIZING"
        )
        self.assertEqual(self.database_state(), after_stage)

        # Crash B: bundle was prepared, but no database commit happened.
        resumed = await self.prepare()
        self.assertEqual(
            resumed.status, "PROTECTED_GET_BATCH_PREPARED_NONAUTHORIZING"
        )
        self.assertEqual(self.database_state(), after_stage)
        self.assertEqual(self.deal_reads, 4)
        self.assertFalse(resumed.mexc_orders_sent)

    async def test_unverified_exchange_outcome_preserves_submitting(self):
        for problem in ("missing_order", "missing_stop", "short_fill", "timeout"):
            with self.subTest(problem=problem):
                await self.asyncSetUp()
                if problem == "missing_order":
                    self.venue.orders.clear()
                if problem == "missing_stop":
                    self.venue.first_stops.pop()
                    self.venue.last_stops.pop()
                if problem == "short_fill":
                    self.deal_records[0]["vol"] = "249"
                if problem == "timeout":
                    async def unavailable(_):
                        raise TimeoutError("private raw payload not exposed")
                    self.venue.order_deal_details = unavailable
                before = self.database_state()
                result = await self.prepare()
                self.assertIsNone(result.prepared_batch)
                self.assertNotEqual(
                    result.status,
                    "PROTECTED_GET_BATCH_PREPARED_NONAUTHORIZING",
                )
                self.assertFalse(result.entry_allowed)
                self.assertFalse(result.mexc_orders_sent)
                self.assertEqual(self.database_state(), before)

    async def test_each_commit_fault_rolls_back_then_new_read_can_recover(self):
        for point in (
            "ACK", "FILL_SET", "FILL", "CHECKPOINT", "REVISION",
            "SLOT_ACK", "SLOT_PROTECTED",
        ):
            with self.subTest(point=point):
                await self.asyncSetUp()
                bundle = (await self.prepare()).prepared_batch
                self.assertIsNotNone(bundle)
                before = self.database_state()
                self.pg.failure = point
                with self.assertRaises(ProtectedCommitHalted):
                    await self.persist(bundle)
                self.assertEqual(self.database_state(), before)
                self.pg.failure = None
                # A subsequent read-only bookend may re-establish evidence.
                refreshed = await self.prepare()
                self.assertIsNotNone(refreshed.prepared_batch)
                self.assertEqual(
                    (await self.persist(refreshed.prepared_batch)).entries_committed,
                    1,
                )
                self.assertEqual(self.pg.new_intents[0]["state"], "ACK")
                self.assertEqual(len(self.pg.committed_fills), 1)

    async def test_unknown_latch_and_stale_fence_cannot_commit(self):
        prepared = await self.prepare()
        self.assertIsNotNone(prepared.prepared_batch)
        self.pg.new_intents[0]["state"] = "UNKNOWN"
        self.pg.reserved[0]["state"] = "UNKNOWN"
        before = self.database_state()
        with self.assertRaises(ProtectedCommitHalted):
            await self.persist(prepared.prepared_batch)
        self.assertEqual(self.database_state(), before)
        self.assertEqual(self.pg.committed_fills, {})

        await self.asyncSetUp()
        prepared = await self.prepare()
        self.pg.lease_token = lease().fencing_token + 1
        before = self.database_state()
        with self.assertRaises(ProtectedCommitHalted):
            await self.persist(prepared.prepared_batch)
        self.assertEqual(self.database_state(), before)

    def test_no_order_post_path_is_exercised(self):
        import inspect
        import app.t100_live_four_slot_protected_get_evidence as bridge
        text = inspect.getsource(bridge)
        self.assertNotIn("._private_post(", text)
        self.assertNotIn("submit_short_with_stop(", text)
        self.assertNotIn(".persist(", text)


if __name__ == "__main__":
    unittest.main()
