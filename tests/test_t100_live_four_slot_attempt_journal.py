"""Offline crash/rollback tests of four-slot attempt journal. Zero MEXC POSTs.

Fake SQL transactions roll back on failure and serialize contenders with
the same advisory lock. Actual PostgreSQL transaction semantics still need
a separate non-production integration test before migration 031 deployment.
"""
from __future__ import annotations

import asyncio
import copy
import unittest
from dataclasses import replace
from datetime import datetime, timezone
from decimal import Decimal as D

from app.t100_live_execution import EntryIntent
from app.t100_live_four_slot_attempt_journal import (
    FourSlotAttemptHalted, PostgresFourSlotAttemptJournal,
    SLOT_ROW_SQL, PREPARE_INTENT_SQL, INTENT_SUBMITTING_SQL,
    SLOT_SUBMITTING_SQL, INTENT_UNKNOWN_SQL, SLOT_UNKNOWN_SQL,
)
from app.t100_live_four_slot_reservations import PostgresFourSlotReservations
from tests.test_t100_live_four_slot_reservations import (
    FakeConn, FakePool, FakeTx, lease,
)
from tests.test_t100_live_four_slot_snapshot_coordinator import (
    Completeness, Ledger, Venue, fixture, report,
)
from app.t100_live_four_slot_snapshot_coordinator import FourSlotSnapshotCoordinator
from app.t100_live_four_slot_planner import plan_four_slot_v40
from tests.test_t100_live_four_slot_planner import prior, OPEN_TS


class AttemptTx(FakeTx):
    async def __aenter__(self):
        self.initial_reservations = copy.deepcopy(self.conn.pg.reserved)
        self.initial_intents = copy.deepcopy(self.conn.pg.new_intents)
        return await super().__aenter__()

    async def __aexit__(self, exc_type, exc, tb):
        if exc_type is not None:
            self.conn.pg.reserved = self.initial_reservations
            self.conn.pg.new_intents = self.initial_intents
        return await super().__aexit__(exc_type, exc, tb)


class AttemptConn(FakeConn):
    def transaction(self, *, isolation):
        return AttemptTx(self, isolation)

    async def fetchrow(self, query, *args):
        if query == SLOT_ROW_SQL:
            self._require_lock()
            return next((copy.deepcopy(row) for row in self.pg.reserved
                         if row["signal_id"] == args[0]), None)
        return await super().fetchrow(query)

    async def fetchval(self, query, *args):
        if query == PREPARE_INTENT_SQL:
            self._require_lock()
            sid, sym, oid, ref, notional, vol, stop = args
            if any(row["signal_id"] == sid or row["external_oid"] == oid
                   for row in self.pg.new_intents):
                return None
            row = {"id": len(self.pg.new_intents)+1, "signal_id": sid,
                   "symbol": sym, "external_oid": oid, "reference_price": ref,
                   "notional_usdt": notional, "volume_contracts": vol,
                   "stop_price": stop, "state": "PREPARED"}
            self.pg.new_intents.append(row)
            return row["id"]
        if query == INTENT_SUBMITTING_SQL:
            self._require_lock()
            oid, sid = args
            row = next((x for x in self.pg.new_intents if x["external_oid"] == oid
                        and x["signal_id"] == sid and x["state"] == "PREPARED"), None)
            if row is None:
                return None
            row["state"] = "SUBMITTING"
            return row["id"]
        if query == SLOT_SUBMITTING_SQL:
            self._require_lock()
            sid, oid, holder, token = args
            if self.pg.fail_slot_cas:
                return None
            row = next((x for x in self.pg.reserved if x["signal_id"] == sid
                        and x["external_oid"] == oid and x["holder_id"] == holder
                        and x["fencing_token"] == token and x["state"] == "RESERVED"), None)
            if row is None:
                return None
            row["state"] = "SUBMITTING"
            return sid
        if query == INTENT_UNKNOWN_SQL:
            self._require_lock()
            sid, oid = args
            row = next((x for x in self.pg.new_intents if x["signal_id"] == sid
                        and x["external_oid"] == oid and x["state"] == "SUBMITTING"), None)
            if row is None:
                return None
            row["state"] = "UNKNOWN"
            return row["id"]
        if query == SLOT_UNKNOWN_SQL:
            self._require_lock()
            sid, oid = args
            if self.pg.fail_unknown_cas:
                return None
            row = next((x for x in self.pg.reserved if x["signal_id"] == sid
                        and x["external_oid"] == oid and x["state"] == "SUBMITTING"), None)
            if row is None:
                return None
            row["state"] = "UNKNOWN"
            return sid
        return await super().fetchval(query, *args)


class AttemptAcquire:
    def __init__(self, pg):
        self.pg = pg

    async def __aenter__(self):
        return AttemptConn(self.pg)

    async def __aexit__(self, *args):
        return False


class AttemptPool(FakePool):
    def __init__(self, snapshot):
        super().__init__(snapshot)
        self.new_intents = []
        self.fail_slot_cas = False
        self.fail_unknown_cas = False

    def acquire(self):
        return AttemptAcquire(self)


class AttemptJournalTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        await self.reset()

    async def reset(self, slots=0):
        snapshot, orders, positions, stops = fixture(slots)
        self.witness = await FourSlotSnapshotCoordinator(
            ledger=Ledger(snapshot),
            venue=Venue(orders, positions, stops),
            completeness=Completeness(report(slots)),
        ).inspect()
        self.pg = AttemptPool(snapshot)
        self.store = PostgresFourSlotReservations(self.pg)
        self.journal = PostgresFourSlotAttemptJournal(self.pg)
        self.observed_at = datetime.now(timezone.utc)

    async def reserve(self, sid=101, sym="NEW_USDT"):
        plan = plan_four_slot_v40(
            inventory=self.witness.inventory,
            durable_recovery=self.witness.recovery,
            symbol=sym,
            prior_bars=prior(), admission_open_ts=OPEN_TS,
            stage1_atr7_pct=6., equity_usdt=10000.,
            marked_existing_usdt=0.,
        )
        result = await self.store.reserve(
            plan=plan, witness=self.witness,
            observed_at=self.observed_at,
            lease=lease(), signal_id=sid, symbol=sym,
        )
        # Fake old reservation store only retains columns needed for its
        # own regression tests. Populate full SQL row for journal tests.
        row = self.pg.reserved[-1]
        row.update({
            "external_oid": result.external_oid,
            "modeled_notional_usdt": result.modeled_notional_usdt,
            "ledger_sha256": self.witness.ledger_hash,
            "inventory_sha256": self.witness.critical_inventory_hash,
            "snapshot_observed_at": self.observed_at,
            "holder_id": lease().holder_id,
            "fencing_token": lease().fencing_token,
        })
        return result

    def intent(self, r):
        return EntryIntent(
            signal_id=r.signal_id, symbol=r.symbol,
            reference_price=D("2"), notional_usdt=D("500"),
            volume_contracts=D("250"), stop_price=D("3.5"),
            external_oid=r.external_oid,
        )

    def params(self, r):
        return dict(
            intent=self.intent(r), witness=self.witness, lease=lease(),
            observed_at=self.observed_at, contract_size=D("1"),
            volume_step=D("1"), price_tick=D("0.01"),
        )

    async def test_stage_persists_intent_and_slot_atomically_without_post(self):
        r = await self.reserve()
        result = await self.journal.stage(**self.params(r))
        self.assertEqual(result.state, "SUBMITTING")
        self.assertFalse(result.post_attempted)
        self.assertFalse(result.entry_allowed)
        self.assertFalse(result.live_authorization)
        self.assertTrue(result.reconciliation_required)
        self.assertEqual(self.pg.reserved[0]["state"], "SUBMITTING")
        self.assertEqual(self.pg.new_intents[0]["state"], "SUBMITTING")
        self.assertEqual(self.pg.locks, 2)

    async def test_duplicate_stage_never_rearms_order_attempt(self):
        r = await self.reserve()
        await self.journal.stage(**self.params(r))
        with self.assertRaisesRegex(FourSlotAttemptHalted, "OWNERSHIP_INVALID"):
            await self.journal.stage(**self.params(r))
        self.assertEqual(len(self.pg.new_intents), 1)

    async def test_four_concurrent_stage_requests_allow_only_one_unresolved(self):
        reservations = [
            await self.reserve(1000+i, f"SIGNAL{i}_USDT")
            for i in range(4)
        ]
        results = await asyncio.gather(
            *(self.journal.stage(**self.params(r)) for r in reservations),
            return_exceptions=True,
        )
        self.assertEqual(sum(not isinstance(x, Exception) for x in results), 1)
        self.assertEqual(len(self.pg.new_intents), 1)
        self.assertEqual(sum(r["state"] == "SUBMITTING" for r in self.pg.reserved), 1)

    async def test_failed_slot_cas_rolls_back_prepared_intent(self):
        r = await self.reserve()
        self.pg.fail_slot_cas = True
        with self.assertRaisesRegex(FourSlotAttemptHalted, "ATOMIC_ATTEMPT_LATCH_FAILED"):
            await self.journal.stage(**self.params(r))
        self.assertEqual(self.pg.new_intents, [])
        self.assertEqual(self.pg.reserved[0]["state"], "RESERVED")

    async def test_unknown_latch_is_atomic_and_irreversible(self):
        r = await self.reserve()
        await self.journal.stage(**self.params(r))
        await self.journal.latch_unknown(
            signal_id=r.signal_id, external_oid=r.external_oid, lease=lease(),
        )
        self.assertEqual(self.pg.new_intents[0]["state"], "UNKNOWN")
        self.assertEqual(self.pg.reserved[0]["state"], "UNKNOWN")
        with self.assertRaises(FourSlotAttemptHalted):
            await self.journal.stage(**self.params(r))
        with self.assertRaises(FourSlotAttemptHalted):
            await self.journal.latch_unknown(
                signal_id=r.signal_id, external_oid=r.external_oid, lease=lease(),
            )

    async def test_unknown_slot_write_failure_rolls_back_intent_unknown(self):
        r = await self.reserve()
        await self.journal.stage(**self.params(r))
        self.pg.fail_unknown_cas = True
        with self.assertRaisesRegex(FourSlotAttemptHalted, "UNKNOWN_ATOMIC_LATCH_FAILED"):
            await self.journal.latch_unknown(
                signal_id=r.signal_id, external_oid=r.external_oid, lease=lease(),
            )
        self.assertEqual(self.pg.new_intents[0]["state"], "SUBMITTING")
        self.assertEqual(self.pg.reserved[0]["state"], "SUBMITTING")

    async def test_stale_leader_prevents_intent_insert(self):
        r = await self.reserve()
        self.pg.lease_token = 12
        with self.assertRaisesRegex(FourSlotAttemptHalted, "FENCING_INVALID"):
            await self.journal.stage(**self.params(r))
        self.assertEqual(self.pg.new_intents, [])

    async def test_changed_ledger_hash_prevents_intent_insert(self):
        r = await self.reserve()
        self.pg.snapshot.checkpoints.append({"symbol": "INJECTED_USDT"})
        with self.assertRaisesRegex(FourSlotAttemptHalted, "LEDGER_DRIFT"):
            await self.journal.stage(**self.params(r))
        self.assertEqual(self.pg.new_intents, [])

    async def test_size_or_native_stop_difference_fails_before_db(self):
        r = await self.reserve()
        base = self.params(r)
        for changed in (
            replace(base["intent"], stop_price=D("3.49")),
            replace(base["intent"], volume_contracts=D("251")),
            replace(base["intent"], notional_usdt=D("7000")),
        ):
            with self.subTest(changed=changed), self.assertRaises(FourSlotAttemptHalted):
                await self.journal.stage(**{**base, "intent": changed})
        self.assertEqual(self.pg.new_intents, [])

    async def test_price_tick_and_contract_lot_check(self):
        r = await self.reserve()
        args = self.params(r)
        with self.assertRaisesRegex(FourSlotAttemptHalted, "PRICE_LOT_OR_STOP"):
            await self.journal.stage(**{**args, "volume_step": D("3")})

    async def test_expired_exchange_snapshot_prevents_intent_insert(self):
        r = await self.reserve()
        self.pg.age_seconds = 5
        with self.assertRaisesRegex(FourSlotAttemptHalted, "SNAPSHOT_STALE"):
            await self.journal.stage(**self.params(r))
        self.assertEqual(self.pg.new_intents, [])

    async def test_unknown_other_order_blocks_new_stage(self):
        old = await self.reserve(99, "OTHER_USDT")
        new = await self.reserve(100, "NEW_USDT")
        self.pg.reserved[0]["state"] = "UNKNOWN"
        with self.assertRaisesRegex(FourSlotAttemptHalted, "OTHER_ORDER_OUTCOME_UNRESOLVED"):
            await self.journal.stage(**self.params(new))
        self.assertEqual(self.pg.new_intents, [])

    async def test_missing_or_drifted_reservation_identity_blocks(self):
        r = await self.reserve()
        args = self.params(r)
        args["intent"] = replace(args["intent"], external_oid="forged-id")
        with self.assertRaisesRegex(FourSlotAttemptHalted, "OWNERSHIP_INVALID"):
            await self.journal.stage(**args)
        self.assertEqual(self.pg.new_intents, [])

    async def test_wrong_database_error_redacts_sensitive_info(self):
        r = await self.reserve()
        self.pg.fail_db = True
        with self.assertRaisesRegex(FourSlotAttemptHalted, "ATTEMPT_DB_FAILED") as caught:
            await self.journal.stage(**self.params(r))
        self.assertNotIn("private DB secret", str(caught.exception))


if __name__ == "__main__":
    unittest.main()
