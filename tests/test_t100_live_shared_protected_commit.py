"""Unwired shared-account V40 atomic DB protected commit transaction tests.

2 stopless operator positions plus ONE new T100 1x CROSS short; the fake DB
contains T100 records only. This is not proof of real PostgreSQL/MEXC
atomicity. No real exchange requests, Render writes or migration execution.
"""
from __future__ import annotations

import copy
import unittest
from dataclasses import replace
from datetime import datetime, timezone
from decimal import Decimal as D

from app.t100_live_v40_protected_commit import (
    PostgresProtectedV40Commit, ProtectedCommitHalted,
)
from tests.test_t100_live_four_slot_attempt_journal import AttemptPool
from tests.test_t100_live_four_slot_reservations import lease
from tests.test_t100_live_four_slot_snapshot_coordinator import fixture
from tests.test_t100_live_shared_protected_finalization import (
    SharedProtectedFinalizationTests,
)
from tests.test_t100_live_v40_protected_commit import CommitPool


class SharedProtectedCommitTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        source = SharedProtectedFinalizationTests()
        await source.asyncSetUp()
        self.batch = source.prepare()
        self.source = source.source
        self.before = self.source.pre_witness
        self.positions = copy.deepcopy(self.source.pre_positions)
        self.stops = copy.deepcopy(self.source.pre_stops)
        self.observed_at = datetime.now(timezone.utc)
        empty, _, _, _ = fixture(0)
        self.pg = AttemptPool(empty)
        self.pg.committed_fill_sets = {}
        self.pg.committed_fills = {}
        self.pg.committed_checkpoints = {}
        self.pg.committed_revisions = {}
        self.pg.failure = None
        self.store = PostgresProtectedV40Commit(CommitPool(self.pg))
        i = self.batch.entries[0].intent
        self.pg.new_intents = [{
            "id": 1, "signal_id": i.signal_id,
            "symbol": i.symbol, "external_oid": i.external_oid,
            "reference_price": i.reference_price,
            "notional_usdt": i.notional_usdt,
            "volume_contracts": i.volume_contracts,
            "stop_price": i.stop_price,
            "state": "SUBMITTING", "exchange_order_id": None,
        }]
        self.pg.reserved = [{
            "signal_id": i.signal_id, "symbol": i.symbol,
            "external_oid": i.external_oid, "state": "SUBMITTING",
            "modeled_notional_usdt": D("600"),
            "ledger_sha256": self.before.ledger_hash,
            "inventory_sha256": self.before.critical_inventory_hash,
            "snapshot_observed_at": self.observed_at,
            "holder_id": lease().holder_id,
            "fencing_token": lease().fencing_token,
        }]

    def snapshot(self):
        return copy.deepcopy((
            self.pg.new_intents, self.pg.reserved,
            self.pg.committed_fill_sets,
            self.pg.committed_fills,
            self.pg.committed_checkpoints,
            self.pg.committed_revisions,
        ))

    async def commit(self, **changes):
        kwargs = dict(
            batch=self.batch,
            pre_attempt_witness=self.before,
            lease=lease(),
            observed_at=self.observed_at,
            shared_pre_positions=self.positions,
            shared_pre_stops=self.stops,
        )
        kwargs.update(changes)
        return await self.store.persist(**kwargs)

    async def test_one_real_t100_checkpoint_two_manual_positions_db_atomic(self):
        before_manual = copy.deepcopy((self.positions, self.stops))
        result = await self.commit()
        self.assertEqual(
            result.status,
            "V40_PROTECTED_RECORDS_ATOMICALLY_PERSISTED_NONAUTHORIZING",
        )
        self.assertEqual(result.entries_committed, 1)
        self.assertTrue(result.db_atomic_commit_observed)
        self.assertFalse(result.venue_state_currently_verified)
        self.assertFalse(result.live_authorization)
        self.assertFalse(result.mexc_orders_sent)
        self.assertEqual(self.pg.new_intents[0]["state"], "ACK")
        self.assertEqual(self.pg.reserved[0]["state"], "PROTECTED")
        self.assertEqual(len(self.pg.committed_checkpoints), 1)
        self.assertEqual(len(self.pg.committed_revisions), 1)
        self.assertEqual(len(self.pg.committed_fills), 1)
        self.assertEqual((self.positions, self.stops), before_manual)

    async def test_every_atomic_write_error_rolls_back_without_retries(self):
        for fault in (
            "ACK", "FILL_SET", "FILL", "CHECKPOINT",
            "REVISION", "SLOT_ACK", "SLOT_PROTECTED",
        ):
            with self.subTest(fault=fault):
                self.pg.failure = fault
                before = self.snapshot()
                with self.assertRaises(ProtectedCommitHalted):
                    await self.commit()
                self.assertEqual(self.snapshot(), before)
        self.pg.failure = None
        await self.commit()
        committed = self.snapshot()
        with self.assertRaises(ProtectedCommitHalted):
            await self.commit()
        self.assertEqual(self.snapshot(), committed)

    async def test_manual_symbols_and_venue_hash_cannot_be_forged(self):
        before = self.snapshot()
        changed = copy.deepcopy(self.positions)
        changed[0]["holdVol"] = D("27")
        with self.assertRaisesRegex(ProtectedCommitHalted, "CRITICAL_HASH_DRIFT"):
            await self.commit(shared_pre_positions=changed)
        self.assertEqual(self.snapshot(), before)
        conflict = replace(
            self.batch.entries[0].intent, symbol="MANUALA_USDT",
        )
        fake_batch = replace(self.batch, entries=(
            replace(self.batch.entries[0], intent=conflict),
        ))
        with self.assertRaises(ProtectedCommitHalted):
            await self.commit(batch=fake_batch)
        self.assertEqual(self.snapshot(), before)

    async def test_reject_manual_only_witness_without_raw_pre_positions(self):
        before = self.snapshot()
        with self.assertRaises(ProtectedCommitHalted):
            await self.commit(shared_pre_positions=None, shared_pre_stops=None)
        with self.assertRaises(ProtectedCommitHalted):
            await self.commit(shared_pre_positions=None)
        self.assertEqual(self.snapshot(), before)

    async def test_preexisting_manual_account_counts_constrain_slots(self):
        before = self.snapshot()
        changed = replace(
            self.before, account_positions_observed=4,
            manual_positions_observed=4,
        )
        with self.assertRaises(ProtectedCommitHalted):
            await self.commit(pre_attempt_witness=changed)
        self.assertEqual(self.snapshot(), before)
        # Additional in-flight reservation must not turn the account into
        # five positions even if the DB has no manual-position checkpoints.
        self.pg.reserved.extend([
            {
                "signal_id": 502 + idx, "symbol": f"OTHER{idx}_USDT",
                "state": "RESERVED",
            } for idx in range(2)
        ])
        before = self.snapshot()
        with self.assertRaises(ProtectedCommitHalted):
            await self.commit()
        self.assertEqual(self.snapshot(), before)

    async def test_stale_leader_and_unresolved_intents_block_all_writes(self):
        before = self.snapshot()
        self.pg.lease_token = 999
        with self.assertRaises(ProtectedCommitHalted):
            await self.commit()
        self.assertEqual(self.snapshot(), before)
        self.pg.lease_token = lease().fencing_token
        self.pg.new_intents.append({
            "external_oid": "foreign", "state": "UNKNOWN",
        })
        before = self.snapshot()
        with self.assertRaisesRegex(ProtectedCommitHalted, "OTHER_INTENT_UNRESOLVED"):
            await self.commit()
        self.assertEqual(self.snapshot(), before)

    async def test_committed_sql_rows_feed_fresh_shared_restart_observer(self):
        # Not a copied hypothetical checkpoint: construct the read-only
        # post-restart ledger from exact rows written by the atomic SQL mock.
        from app.t100_live_four_slot_snapshot_coordinator import (
            FourSlotSnapshotCoordinator, LedgerSnapshot,
        )
        from app.t100_live_shared_restart_recovery import (
            verify_shared_restart_protected_entry,
        )
        from tests.test_t100_live_four_slot_snapshot_coordinator import (
            Ledger, Venue, Completeness,
        )
        from tests.test_t100_live_shared_protected_entry_observer import counts

        result = await self.commit()
        self.assertEqual(result.entries_committed, 1)
        checkpoint_columns = (
            "position_id", "symbol", "external_oid", "exchange_order_id",
            "contracts", "entry_price", "stop_order_id", "stop_price",
            "best_profit_pct", "armed", "lifecycle_state",
            "checkpoint_version", "last_write_reason",
        )
        revision_columns = (
            "position_id", "checkpoint_version", "write_reason",
            "symbol", "external_oid", "exchange_order_id", "contracts",
            "entry_price", "stop_order_id", "stop_price",
            "best_profit_pct", "armed", "lifecycle_state",
        )
        cp_rows = [
            dict(zip(checkpoint_columns, row))
            for row in self.pg.committed_checkpoints.values()
        ]
        revision_rows = [
            dict(zip(revision_columns, row))
            for row in self.pg.committed_revisions.values()
        ]
        self.assertEqual(len(cp_rows), 1)
        self.assertEqual(len(revision_rows), 1)
        ledger = Ledger(LedgerSnapshot(
            checkpoints=cp_rows,
            revisions=revision_rows,
            intents=copy.deepcopy(self.pg.new_intents),
            stop_amendments=[],
            readonly_repeatable_read=True,
        ))
        original = self.source.order
        fresh = await FourSlotSnapshotCoordinator(
            ledger=ledger,
            venue=Venue(
                [original],
                copy.deepcopy(self.source.post_positions),
                copy.deepcopy(self.source.post_stops),
            ),
            completeness=Completeness(counts(3, 1)),
        ).inspect(shared_manual_observation=True)
        self.assertEqual(
            fresh.status,
            "SHARED_ACCOUNT_MANAGED_PROVENANCE_MATCHED_NONAUTHORIZING",
        )
        self.assertEqual(fresh.occupied_slots, 1)
        self.assertEqual(fresh.manual_positions_observed, 2)
        replay = verify_shared_restart_protected_entry(
            pre_witness=self.before,
            restart_witness=fresh,
            prepared=self.batch,
            pre_positions=self.positions,
            pre_stops=self.stops,
            restart_positions=self.source.post_positions,
            restart_stops=self.source.post_stops,
            origin_order=original,
        )
        self.assertEqual(
            replay.status,
            "SHARED_RESTART_EXACT_PROTECTED_PROVENANCE_OBSERVED_NONAUTHORIZING",
        )
        self.assertTrue(replay.owned_native_stops_recovered)
        self.assertTrue(replay.new_protected_fill_record_matches_recovery)
        self.assertEqual(replay.unchanged_manual_positions, 2)
        self.assertFalse(replay.real_db_commit_independently_verified)
        self.assertFalse(replay.live_authorization)

    async def test_failed_atomic_commit_leaves_no_durable_owned_checkpoint(self):
        self.pg.failure = "REVISION"
        with self.assertRaises(ProtectedCommitHalted):
            await self.commit()
        self.assertEqual(self.pg.committed_checkpoints, {})
        self.assertEqual(self.pg.committed_revisions, {})
        self.assertEqual(self.pg.committed_fill_sets, {})
        self.assertEqual(self.pg.committed_fills, {})
        self.assertEqual(self.pg.new_intents[0]["state"], "SUBMITTING")
        self.assertEqual(self.pg.reserved[0]["state"], "SUBMITTING")

    async def test_manual_tp_only_supported_not_rewritten(self):
        # A manual TP-only record is not adopted or converted into an SL.
        # Re-create a full coordinator witness matching this changed account.
        from app.t100_live_four_slot_snapshot_coordinator import (
            FourSlotSnapshotCoordinator,
        )
        from tests.test_t100_live_four_slot_snapshot_coordinator import (
            Ledger, Venue, Completeness,
        )
        from tests.test_t100_live_shared_protected_entry_observer import counts
        empty, _, _, _ = fixture(0)
        tp = {
            "symbol": "MANUALB_USDT", "positionId": "11",
            "id": "988", "takeProfitPrice": D("5"),
            "stopLossPrice": D("0"),
        }
        witness = await FourSlotSnapshotCoordinator(
            ledger=Ledger(empty),
            venue=Venue([], self.positions, [tp]),
            completeness=Completeness(counts(2, 1)),
        ).inspect(shared_manual_observation=True)
        self.assertEqual(
            witness.status, "SHARED_ACCOUNT_MANUAL_ONLY_NONAUTHORIZING"
        )
        self.before = witness
        self.stops = [tp]
        self.pg.reserved[0]["inventory_sha256"] = witness.critical_inventory_hash
        self.pg.reserved[0]["ledger_sha256"] = witness.ledger_hash
        before_manual = copy.deepcopy((self.positions, self.stops))
        result = await self.commit()
        self.assertTrue(result.db_atomic_commit_observed)
        self.assertEqual((self.positions, self.stops), before_manual)

    def test_no_live_order_or_runtime_wiring(self):
        import inspect
        import app.t100_live_v40_protected_commit as module
        src = inspect.getsource(module)
        for forbidden in (
            "submit_short_with_stop(", "MexcTradeClient(",
            "._private_post(", "LiveMutationPermit", "httpx.",
        ):
            self.assertNotIn(forbidden, src)


if __name__ == "__main__":
    unittest.main()
