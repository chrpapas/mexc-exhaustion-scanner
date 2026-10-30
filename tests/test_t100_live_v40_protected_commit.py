"""Fixture DB transaction integration for protected V40 ACK/fills/checkpoints.

Uses an injected fake database with true rollback snapshots and an account lock.
NO MEXC requests, credentials, production migrations, or live-order settings.
A fake DB is NOT real PostgreSQL transaction/crash certification.
"""
from __future__ import annotations

import copy
from dataclasses import asdict
from datetime import datetime, timezone
from decimal import Decimal as D
import unittest

from app.t100_live_v40_protected_commit import (
    ACK_INTENT_SQL, CURRENT_INTENT_SQL, INSERT_CHECKPOINT_SQL, INSERT_FILL_SQL,
    INSERT_FILL_SET_SQL, INSERT_REVISION_SQL, SLOT_ACK_SQL,
    SLOT_PROTECTED_SQL, UNRESOLVED_IDENTITIES_SQL,
    PostgresProtectedV40Commit, ProtectedCommitHalted,
)
from app.t100_live_four_slot_attempt_journal import SLOT_ROW_SQL
from app.t100_live_four_slot_reservations import (
    CHECKPOINT_IDS_SQL, RESERVATIONS_SQL,
)
from app.t100_live_durable_recovery import recover_nonflat
from app.t100_live_v40_protected_finalization import prepare_protected_entries
from tests.test_t100_live_four_slot_attempt_outcome import ProtectedOutcomeTests
from tests.test_t100_live_four_slot_attempt_journal import (
    AttemptConn, AttemptTx, lease,
)
from tests.test_t100_live_four_slot_snapshot_coordinator import fixture
from tests.test_t100_live_durable_recovery import (
    revisions_for, intent as historical_intent,
)
from tests.test_t100_live_v40_protected_finalization import deal


class CommitTx(AttemptTx):
    async def __aenter__(self):
        await super().__aenter__()
        self.backup = copy.deepcopy((
            self.conn.pg.committed_fill_sets, self.conn.pg.committed_fills,
            self.conn.pg.committed_checkpoints,
            self.conn.pg.committed_revisions,
        ))
        return self

    async def __aexit__(self, typ, err, tb):
        if typ is not None:
            (
                self.conn.pg.committed_fill_sets,
                self.conn.pg.committed_fills,
                self.conn.pg.committed_checkpoints,
                self.conn.pg.committed_revisions,
            ) = self.backup
        return await super().__aexit__(typ, err, tb)


class CommitConn(AttemptConn):
    def transaction(self, *, isolation):
        return CommitTx(self, isolation)

    async def fetch(self, query, *args):
        if query == UNRESOLVED_IDENTITIES_SQL:
            self._require_lock()
            return [
                {"external_oid": row["external_oid"], "state": row["state"]}
                for row in self.pg.new_intents
                if row["state"] in {"PREPARED", "SUBMITTING", "UNKNOWN", "REVIEW"}
            ]
        return await super().fetch(query, *args)

    async def fetchrow(self, query, *args):
        if query == CURRENT_INTENT_SQL:
            self._require_lock()
            return next((
                copy.deepcopy(row) for row in self.pg.new_intents
                if row["external_oid"] == args[0]
            ), None)
        return await super().fetchrow(query, *args)

    async def fetchval(self, query, *args):
        self._require_lock()
        if query == ACK_INTENT_SQL:
            oid, sid, orderid = args
            row = next((
                r for r in self.pg.new_intents if
                r["external_oid"] == oid and r["signal_id"] == sid
                and r["state"] == "SUBMITTING"
                and r.get("exchange_order_id") is None
            ), None)
            if row is None or self.pg.failure == "ACK":
                return None
            row["state"], row["exchange_order_id"] = "ACK", orderid
            return row["id"]
        if query == INSERT_FILL_SET_SQL:
            order_id = args[0]
            if self.pg.failure == "FILL_SET" or order_id in self.pg.committed_fill_sets:
                return None
            self.pg.committed_fill_sets[order_id] = args
            return order_id
        if query == INSERT_FILL_SQL:
            trade_id = args[0]
            if self.pg.failure == "FILL" or trade_id in self.pg.committed_fills:
                return None
            self.pg.committed_fills[trade_id] = args
            return trade_id
        if query == INSERT_CHECKPOINT_SQL:
            pid = args[0]
            if self.pg.failure == "CHECKPOINT" or pid in self.pg.committed_checkpoints:
                return None
            self.pg.committed_checkpoints[pid] = args
            return pid
        if query == INSERT_REVISION_SQL:
            if self.pg.failure == "REVISION":
                return None
            pid, version = args[:2]
            key = (pid, version)
            if key in self.pg.committed_revisions:
                return None
            self.pg.committed_revisions[key] = args
            return len(self.pg.committed_revisions)
        if query in (SLOT_ACK_SQL, SLOT_PROTECTED_SQL):
            sid, oid, holder, token = args
            target = "ACK" if query == SLOT_ACK_SQL else "PROTECTED"
            before = "SUBMITTING" if target == "ACK" else "ACK"
            row = next((r for r in self.pg.reserved
                        if r["signal_id"] == sid and r["external_oid"] == oid
                        and r["holder_id"] == holder and r["fencing_token"] == token
                        and r["state"] == before), None)
            if row is None or self.pg.failure == "SLOT_" + target:
                return None
            row["state"] = target
            return sid
        return await super().fetchval(query, *args)


class CommitAcquire:
    def __init__(self, pg): self.pg = pg
    async def __aenter__(self): return CommitConn(self.pg)
    async def __aexit__(self, *exc): return False


class CommitPool:
    def __init__(self, pg):
        self.pg = pg
    def acquire(self):
        return CommitAcquire(self.pg)


class ProtectedCommitTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        prior = ProtectedOutcomeTests()
        await prior.asyncSetUp()
        self.pg = prior.pg
        self.pg.committed_fill_sets = {}
        self.pg.committed_fills = {}
        self.pg.committed_checkpoints = {}
        self.pg.committed_revisions = {}
        self.pg.failure = None
        self.store = PostgresProtectedV40Commit(CommitPool(self.pg))
        self.prior = prior
        self.order = copy.deepcopy(prior.venue.orders[(
            "FOURTH_USDT", prior.intent.external_oid,
        )])
        self.order["feeCurrency"] = "USDT"
        self.position_rows = copy.deepcopy(prior.venue.first_positions)
        self.stop_rows = copy.deepcopy(prior.venue.first_stops)
        self.inventory = copy.deepcopy(prior.complete.result)
        self.batch = prepare_protected_entries(
            stages=[prior.stage], intents=[prior.intent],
            pre_attempt_witness=prior.before,
            orders=[self.order], positions=self.position_rows,
            stops=self.stop_rows, inventory=self.inventory,
            deals_by_order_id={"990": [deal()]},
        )

    async def commit(self):
        return await self.store.persist(
            batch=self.batch,
            pre_attempt_witness=self.prior.before,
            lease=lease(), observed_at=datetime.now(timezone.utc),
        )

    def snapshot(self):
        return copy.deepcopy((
            self.pg.reserved, self.pg.new_intents,
            self.pg.committed_fill_sets, self.pg.committed_fills,
            self.pg.committed_checkpoints, self.pg.committed_revisions,
        ))

    async def test_protected_fourth_slot_records_commit_atomically(self):
        result = await self.commit()
        self.assertEqual(
            result.status,
            "V40_PROTECTED_RECORDS_ATOMICALLY_PERSISTED_NONAUTHORIZING",
        )
        self.assertEqual(result.entries_committed, 1)
        self.assertTrue(result.db_atomic_commit_observed)
        self.assertFalse(result.venue_state_currently_verified)
        self.assertFalse(result.live_authorization)
        self.assertFalse(result.real_post_attempted)
        self.assertEqual(self.pg.new_intents[0]["state"], "ACK")
        self.assertEqual(self.pg.new_intents[0]["exchange_order_id"], "990")
        self.assertEqual(self.pg.reserved[0]["state"], "PROTECTED")
        self.assertEqual(len(self.pg.committed_fill_sets), 1)
        self.assertEqual(len(self.pg.committed_fills), 1)
        self.assertEqual(len(self.pg.committed_checkpoints), 1)
        self.assertEqual(len(self.pg.committed_revisions), 1)

    async def test_failure_in_any_atomic_step_rolls_everything_back(self):
        for failure in ("ACK", "FILL_SET", "FILL", "CHECKPOINT",
                        "REVISION", "SLOT_ACK", "SLOT_PROTECTED"):
            with self.subTest(failure=failure):
                self.pg.failure = failure
                before = self.snapshot()
                with self.assertRaises(ProtectedCommitHalted):
                    await self.commit()
                self.assertEqual(self.snapshot(), before)
        self.pg.failure = None
        self.assertEqual((await self.commit()).entries_committed, 1)

    async def test_retries_after_commit_fail_closed_no_extra_fills(self):
        await self.commit()
        snapshot = self.snapshot()
        with self.assertRaises(ProtectedCommitHalted):
            await self.commit()
        self.assertEqual(self.snapshot(), snapshot)

    async def test_stale_fence_and_other_unknown_order_block_before_write(self):
        before = self.snapshot()
        self.pg.lease_token = 100
        with self.assertRaises(ProtectedCommitHalted):
            await self.commit()
        self.assertEqual(self.snapshot(), before)
        self.pg.lease_token = lease().fencing_token
        self.pg.new_intents.append({
            "id": 32, "external_oid": "outside", "state": "UNKNOWN",
        })
        before = self.snapshot()
        with self.assertRaisesRegex(ProtectedCommitHalted, "OTHER_INTENT_UNRESOLVED"):
            await self.commit()
        self.assertEqual(self.snapshot(), before)

    async def test_synthetic_four_position_restart_reconstruction_after_commit(self):
        await self.commit()
        snap, old_orders, positions, stops = fixture(3)
        cp = self.batch.entries[0].checkpoint
        record = asdict(cp)
        # Existing recovery validator reads the same OPEN/ACK and revision
        # semantics that the production SELECT-only ledger reader projects.
        rev = {
            **record, "write_reason": "ENTRY_PROTECTED",
        }
        snap.checkpoints.append(record)
        snap.revisions.append(rev)
        snap.intents.append(historical_intent(
            symbol=cp.symbol, external_oid=cp.external_oid,
            exchange_order_id=cp.exchange_order_id,
            volume_contracts=cp.contracts, stop_price=self.prior.intent.stop_price,
            state="ACK",
        ))
        old_orders.append(self.order)
        result = recover_nonflat(
            checkpoint_rows=snap.checkpoints,
            revision_rows=snap.revisions,
            intent_rows=snap.intents,
            stop_amendment_rows=snap.stop_amendments,
            orders=old_orders,
            positions=self.position_rows,
            stops=self.stop_rows,
            positions_complete=True, stops_complete=True,
        )
        self.assertEqual(len(result.recovered), 4)
        self.assertFalse(result.live_authorization)

    def test_only_database_adapter_no_mexc_order_transport(self):
        import inspect
        import app.t100_live_v40_protected_commit as module
        source = inspect.getsource(module)
        for forbidden in (
            "submit_short_with_stop(", "MexcTradeClient(", "._private_post(",
            "LiveMutationPermit", "httpx.",
        ):
            self.assertNotIn(forbidden, source)


if __name__ == "__main__":
    unittest.main()
