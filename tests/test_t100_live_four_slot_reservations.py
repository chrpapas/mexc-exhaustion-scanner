"""Offline PostgreSQL transaction/leader fencing + 4-slot contention tests.

Fake pool models the account advisory lock and row persistence. Real PostgreSQL
serializability and MEXC atomicity are NOT certified by this fake. No network.
"""
from __future__ import annotations

import asyncio
import unittest
from dataclasses import replace
from datetime import datetime, timezone

from app.t100_live_db_inspect import EXPECTED_DATABASE
from app.t100_live_four_slot_planner import plan_four_slot_v40
from app.t100_live_four_slot_reservations import (
    ACCOUNT_LOCK_SQL, AMENDMENTS_SQL, CHECKPOINTS_SQL,
    CHECKPOINT_IDS_SQL, INSERT_SQL, INTENTS_SQL,
    LEASE_SQL, LEGACY_CHILDREN_SQL, LEGACY_PARENTS_SQL,
    RESERVATIONS_SQL, REVISIONS_SQL, SNAPSHOT_AGE_SQL,
    UNRESOLVED_INTENTS_SQL, UNRESOLVED_STOPS_SQL,
    FourSlotReservationHalted, PostgresFourSlotReservations,
)
from app.t100_live_four_slot_snapshot_coordinator import FourSlotSnapshotCoordinator
from app.t100_live_leader_fencing import LeaderLease
from tests.test_t100_live_four_slot_planner import OPEN_TS, prior
from tests.test_t100_live_four_slot_snapshot_coordinator import (
    Completeness, Ledger, Venue, fixture, report,
)


def lease(token=11):
    return LeaderLease(
        lease_name="t100-live-executor", holder_id="worker-A",
        fencing_token=token, ttl_seconds=45,
    )


class FakeTx:
    def __init__(self, conn, isolation):
        self.conn = conn
        self.isolation = isolation

    async def __aenter__(self):
        self.conn.started = True
        self.conn.pg.modes.append(self.isolation)
        return self

    async def __aexit__(self, *args):
        if self.conn.locked:
            self.conn.pg.lock.release()
        self.conn.locked = False
        self.conn.started = False


class FakeConn:
    def __init__(self, pg):
        self.pg = pg
        self.started = False
        self.locked = False

    def transaction(self, *, isolation):
        return FakeTx(self, isolation)

    async def execute(self, query):
        if query != ACCOUNT_LOCK_SQL or not self.started:
            raise AssertionError("account advisory lock must be transaction first")
        await self.pg.lock.acquire()
        self.locked = True
        self.pg.locks += 1

    def _require_lock(self):
        if not self.started or not self.locked:
            raise AssertionError("DB read/write without global lock")

    async def fetchrow(self, query):
        self._require_lock()
        if query != LEASE_SQL:
            raise AssertionError("unexpected fetchrow query")
        if self.pg.unavailable_lease:
            return None
        return {
            "holder_id": self.pg.lease_holder,
            "fencing_token": self.pg.lease_token,
            "unexpired": self.pg.lease_unexpired,
        }

    async def fetch(self, query):
        self._require_lock()
        if query == CHECKPOINTS_SQL:
            return self.pg.snapshot.checkpoints
        if query == REVISIONS_SQL:
            return self.pg.snapshot.revisions
        if query == INTENTS_SQL:
            return self.pg.snapshot.intents
        if query == AMENDMENTS_SQL:
            return self.pg.snapshot.stop_amendments
        if query == CHECKPOINT_IDS_SQL:
            return [
                {"symbol": row["symbol"], "position_id": row["position_id"]}
                for row in self.pg.snapshot.checkpoints
            ]
        if query == RESERVATIONS_SQL:
            return [dict(x) for x in self.pg.reserved if x["state"] != "RELEASED"]
        raise AssertionError("unexpected SQL SELECT")

    async def fetchval(self, query, *args):
        self._require_lock()
        if self.pg.fail_db:
            raise RuntimeError("private DB secret example")
        if query == "SELECT current_database()":
            return self.pg.database
        if query == SNAPSHOT_AGE_SQL:
            return self.pg.age_seconds
        if query == UNRESOLVED_INTENTS_SQL:
            return self.pg.unresolved_intents
        if query == UNRESOLVED_STOPS_SQL:
            return self.pg.unresolved_stops
        if query == LEGACY_PARENTS_SQL:
            return self.pg.old_parents
        if query == LEGACY_CHILDREN_SQL:
            return self.pg.old_children
        if query == INSERT_SQL:
            (signal_id, symbol, oid, modeled, ledger_hash, venue_hash,
             observed_at, holder, fencing) = args
            if any(x["signal_id"] == signal_id or x["symbol"] == symbol
                   for x in self.pg.reserved):
                raise RuntimeError("uniqueness constraint")
            self.pg.reserved.append({
                "signal_id": signal_id, "symbol": symbol,
                "state": "RESERVED", "oid": oid,
            })
            return signal_id
        raise AssertionError("unexpected fetchval query")


class FakeAcquire:
    def __init__(self, pg):
        self.pg = pg

    async def __aenter__(self):
        return FakeConn(self.pg)

    async def __aexit__(self, *args):
        return False


class FakePool:
    def __init__(self, snapshot):
        self.snapshot = snapshot
        self.lock = asyncio.Lock()
        self.reserved = []
        self.locks = 0
        self.modes = []
        self.database = EXPECTED_DATABASE
        self.lease_holder = "worker-A"
        self.lease_token = 11
        self.lease_unexpired = True
        self.unavailable_lease = False
        self.age_seconds = 0.1
        self.unresolved_intents = 0
        self.unresolved_stops = 0
        self.old_parents = 0
        self.old_children = 0
        self.fail_db = False

    def acquire(self):
        return FakeAcquire(self)


class FourSlotReservationTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        await self.reset(0)

    async def reset(self, n):
        snap, orders, positions, stops = fixture(n)
        witness = await FourSlotSnapshotCoordinator(
            ledger=Ledger(snap),
            venue=Venue(orders, positions, stops),
            completeness=Completeness(report(n)),
        ).inspect()
        self.witness = witness
        self.pg = FakePool(snap)
        self.store = PostgresFourSlotReservations(self.pg)

    def params(self, signal_id, symbol):
        plan = plan_four_slot_v40(
            inventory=self.witness.inventory,
            durable_recovery=self.witness.recovery,
            symbol=symbol, prior_bars=prior(),
            admission_open_ts=OPEN_TS,
            stage1_atr7_pct=6.,
            equity_usdt=10000.,
            marked_existing_usdt=0.,
        )
        return dict(
            plan=plan, witness=self.witness,
            observed_at=datetime.now(timezone.utc),
            lease=lease(), signal_id=signal_id, symbol=symbol,
        )

    async def test_empty_account_reserves_one_without_post_or_authorization(self):
        outcome = await self.store.reserve(**self.params(1001, "NEW_USDT"))
        self.assertEqual(outcome.journal_state, "RESERVED")
        self.assertEqual(outcome.occupied_after_reservation, 1)
        self.assertEqual(outcome.modeled_notional_usdt, 6250)
        self.assertTrue(outcome.external_oid.startswith("t100v40r"))
        self.assertFalse(outcome.post_attempted)
        self.assertFalse(outcome.entry_allowed)
        self.assertFalse(outcome.live_authorization)
        self.assertEqual(self.pg.modes, ["read_committed"])
        self.assertEqual(self.pg.locks, 1)

    async def test_three_owned_positions_allow_one_more_reservation(self):
        await self.reset(3)
        out = await self.store.reserve(**self.params(1001, "NEW_USDT"))
        self.assertEqual(out.occupied_after_reservation, 4)

    async def test_two_contenders_for_last_slot_only_one_wins(self):
        await self.reset(3)
        results = await asyncio.gather(
            self.store.reserve(**self.params(1001, "NEW_USDT")),
            self.store.reserve(**self.params(1002, "NEXT_USDT")),
            return_exceptions=True,
        )
        self.assertEqual(sum(not isinstance(x, Exception) for x in results), 1)
        self.assertEqual(len(self.pg.reserved), 1)
        self.assertEqual(self.pg.locks, 2)

    async def test_six_contenders_for_empty_account_allow_four(self):
        results = await asyncio.gather(
            *(self.store.reserve(**self.params(1000+i, f"NEW{i}_USDT"))
              for i in range(6)),
            return_exceptions=True,
        )
        self.assertEqual(sum(not isinstance(x, Exception) for x in results), 4)
        self.assertEqual(len(self.pg.reserved), 4)

    async def test_duplicate_symbol_or_signal_refused(self):
        await self.store.reserve(**self.params(1001, "NEW_USDT"))
        for sid, sym in ((1002, "NEW_USDT"), (1001, "OTHER_USDT")):
            with self.subTest(sid=sid, sym=sym), self.assertRaises(
                FourSlotReservationHalted
            ):
                await self.store.reserve(**self.params(sid, sym))
        self.assertEqual(len(self.pg.reserved), 1)

    async def test_unknown_order_blocks_global_new_reservations(self):
        self.pg.reserved.append({
            "signal_id": 1, "symbol": "PREVIOUS_USDT", "state": "UNKNOWN"
        })
        with self.assertRaisesRegex(FourSlotReservationHalted, "UNRESOLVED"):
            await self.store.reserve(**self.params(2, "NEW_USDT"))
        self.assertEqual(len(self.pg.reserved), 1)

    async def test_unresolved_legacy_or_stop_state_blocks(self):
        for key in ("unresolved_intents", "unresolved_stops", "old_parents", "old_children"):
            with self.subTest(key=key):
                setattr(self.pg, key, 1)
                with self.assertRaisesRegex(
                    FourSlotReservationHalted, "OTHER_MUTATION_UNRESOLVED"
                ):
                    await self.store.reserve(**self.params(2, "NEW_USDT"))
                setattr(self.pg, key, 0)

    async def test_stale_leader_fence_blocks(self):
        self.pg.lease_token = 12
        with self.assertRaisesRegex(FourSlotReservationHalted, "LEADER_FENCING_INVALID"):
            await self.store.reserve(**self.params(2, "NEW_USDT"))
        self.assertFalse(self.pg.reserved)

    async def test_expired_or_wrong_leader_blocks(self):
        self.pg.lease_unexpired = False
        with self.assertRaisesRegex(FourSlotReservationHalted, "LEADER_FENCING_INVALID"):
            await self.store.reserve(**self.params(2, "NEW_USDT"))
        self.pg.lease_unexpired = True
        self.pg.lease_holder = "other"
        with self.assertRaisesRegex(FourSlotReservationHalted, "LEADER_FENCING_INVALID"):
            await self.store.reserve(**self.params(2, "NEW_USDT"))

    async def test_forged_or_missing_readonly_witness_blocks_before_db(self):
        values = self.params(2, "NEW_USDT")
        values["witness"] = replace(self.witness, ledger_hash="00"*32)
        with self.assertRaisesRegex(FourSlotReservationHalted, "LEDGER_CHANGED"):
            await self.store.reserve(**values)

    async def test_bad_notional_or_model_blocks_before_db(self):
        values = self.params(2, "NEW_USDT")
        values["plan"] = replace(values["plan"], modeled_notional_usdt=999.)
        with self.assertRaisesRegex(FourSlotReservationHalted, "NOTIONAL_MISMATCH"):
            await self.store.reserve(**values)
        self.assertEqual(self.pg.locks, 0)

    async def test_admitted_symbol_cannot_be_swapped_before_reservation(self):
        values = self.params(2, "NEW_USDT")
        values["symbol"] = "DIFFERENT_USDT"
        with self.assertRaisesRegex(
            FourSlotReservationHalted, "ADMISSION_EVIDENCE_BLOCKED"
        ):
            await self.store.reserve(**values)
        self.assertEqual(self.pg.locks, 0)

    async def test_future_dated_snapshot_blocks(self):
        self.pg.age_seconds = -1.
        with self.assertRaisesRegex(
            FourSlotReservationHalted, "SNAPSHOT_TOO_OLD"
        ):
            await self.store.reserve(**self.params(2, "NEW_USDT"))

    async def test_unowned_position_is_not_reservable(self):
        await self.reset(1)
        values = self.params(2, "NEW_USDT")
        values["witness"] = replace(self.witness, status="UNOWNED_OR_MISSING_CHECKPOINTS")
        with self.assertRaises(FourSlotReservationHalted):
            await self.store.reserve(**values)
        self.assertEqual(self.pg.locks, 0)

    async def test_changed_checkpoint_set_blocks(self):
        await self.reset(3)
        values = self.params(2, "NEW_USDT")
        self.pg.snapshot.checkpoints[0]["stop_price"] = 200
        with self.assertRaisesRegex(FourSlotReservationHalted, "LEDGER_CHANGED"):
            await self.store.reserve(**values)

    async def test_stale_snapshot_blocks(self):
        self.pg.age_seconds = 3.1
        with self.assertRaisesRegex(FourSlotReservationHalted, "SNAPSHOT_TOO_OLD"):
            await self.store.reserve(**self.params(2, "NEW_USDT"))

    async def test_wrong_database_blocks(self):
        self.pg.database = "wrong_db"
        with self.assertRaisesRegex(FourSlotReservationHalted, "WRONG_DATABASE"):
            await self.store.reserve(**self.params(2, "NEW_USDT"))

    async def test_db_failure_redacts_sensitive_error(self):
        self.pg.fail_db = True
        with self.assertRaisesRegex(FourSlotReservationHalted, "RESERVATION_DB_FAILED") as caught:
            await self.store.reserve(**self.params(2, "NEW_USDT"))
        self.assertNotIn("private DB secret", str(caught.exception))

    async def test_protected_reservation_can_coexist_with_its_checkpoint(self):
        await self.reset(1)
        self.pg.reserved = [{
            "signal_id": 222, "symbol": "S0_USDT", "state": "PROTECTED"
        }]
        out = await self.store.reserve(**self.params(2, "NEW_USDT"))
        self.assertEqual(out.occupied_after_reservation, 2)


if __name__ == "__main__":
    unittest.main()
