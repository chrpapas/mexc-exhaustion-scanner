"""No-orders V40 shared-account reserve -> SUBMITTING atomic journal tests.

The fake account has a manual CROSS short and an isolated LONG, both
without native stops. No owner adoption, permit, MEXC write, Render or
production migration. The two database writers still require the same
account advisory lock and active leader fencing token.
"""
from __future__ import annotations

import copy
from datetime import datetime, timezone
from decimal import Decimal as D
import unittest

from app.t100_live_execution import EntryIntent
from app.t100_live_four_slot_attempt_journal import (
    FourSlotAttemptHalted, PostgresFourSlotAttemptJournal,
)
from app.t100_live_four_slot_reservations import (
    FourSlotReservationHalted, PostgresFourSlotReservations,
)
from app.t100_live_four_slot_planner import plan_shared_four_slot_v40
from tests.test_t100_live_four_slot_attempt_journal import AttemptPool
from tests.test_t100_live_four_slot_reservations import lease
from tests.test_t100_live_four_slot_planner import prior, OPEN_TS
from tests.test_t100_live_four_slot_snapshot_coordinator import fixture
from tests.test_t100_live_shared_protected_entry_observer import (
    SharedProtectedEntryTests,
)


class SharedReservationAttemptTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        source = SharedProtectedEntryTests()
        await source.asyncSetUp()
        self.source = source
        self.witness = source.pre_witness
        self.positions = copy.deepcopy(source.pre_positions)
        self.stops = copy.deepcopy(source.pre_stops)
        empty, _, _, _ = fixture(0)
        self.pg = AttemptPool(empty)
        self.reservations = PostgresFourSlotReservations(self.pg)
        self.journal = PostgresFourSlotAttemptJournal(self.pg)
        self.observed_at = datetime.now(timezone.utc)

    def plan(self, symbol="NEW_USDT"):
        return plan_shared_four_slot_v40(
            snapshot=self.witness,
            account_positions=self.positions,
            account_stops=self.stops,
            symbol=symbol,
            prior_bars=prior(),
            admission_open_ts=OPEN_TS,
            stage1_atr7_pct=6.0,
            equity_usdt=10000.,
            marked_existing_usdt=1000.,
        )

    def reserve_args(self, symbol="NEW_USDT", sid=410):
        return dict(
            plan=self.plan(symbol), witness=self.witness,
            shared_pre_positions=self.positions,
            shared_pre_stops=self.stops,
            observed_at=self.observed_at,
            lease=lease(), signal_id=sid, symbol=symbol,
        )

    async def reserve(self, symbol="NEW_USDT", sid=410):
        reserved = await self.reservations.reserve(
            **self.reserve_args(symbol, sid)
        )
        row = self.pg.reserved[-1]
        row.update({
            "external_oid": reserved.external_oid,
            "modeled_notional_usdt": reserved.modeled_notional_usdt,
            "ledger_sha256": self.witness.ledger_hash,
            "inventory_sha256": self.witness.critical_inventory_hash,
            "snapshot_observed_at": self.observed_at,
            "holder_id": lease().holder_id,
            "fencing_token": lease().fencing_token,
        })
        return reserved

    def stage_args(self, reservation):
        intent = EntryIntent(
            signal_id=reservation.signal_id,
            symbol=reservation.symbol,
            reference_price=D("2"),
            notional_usdt=D("500"),
            volume_contracts=D("250"),
            stop_price=D("3.5"),
            external_oid=reservation.external_oid,
        )
        return dict(
            intent=intent, witness=self.witness,
            shared_pre_positions=self.positions,
            shared_pre_stops=self.stops,
            observed_at=self.observed_at,
            lease=lease(), contract_size=D("1"),
            volume_step=D("1"), price_tick=D("0.01"),
        )

    def snapshot(self):
        return copy.deepcopy((self.pg.reserved, self.pg.new_intents))

    async def test_two_stopless_manual_positions_reserve_and_stage_without_orders(self):
        r = await self.reserve()
        self.assertEqual(r.occupied_after_reservation, 3)
        self.assertEqual(r.journal_state, "RESERVED")
        self.assertFalse(r.entry_allowed)
        self.assertFalse(r.live_authorization)
        staged = await self.journal.stage(**self.stage_args(r))
        self.assertEqual(staged.state, "SUBMITTING")
        self.assertFalse(staged.post_attempted)
        self.assertTrue(staged.reconciliation_required)
        self.assertFalse(staged.entry_allowed)
        self.assertFalse(staged.live_authorization)
        self.assertEqual(self.pg.reserved[0]["state"], "SUBMITTING")
        self.assertEqual(self.pg.new_intents[0]["state"], "SUBMITTING")
        self.assertEqual(len(self.pg.new_intents), 1)
        self.assertEqual(len(self.positions), 2)
        self.assertEqual(self.stops, [])
        self.assertEqual(self.pg.modes, ["read_committed", "read_committed"])
        self.assertEqual(self.pg.locks, 2)

    async def test_manual_symbol_conflicts_and_original_flat_path_still_block(self):
        for symbol in ("MANUALA_USDT", "MANUALB_USDT"):
            with self.subTest(symbol=symbol):
                with self.assertRaises(FourSlotReservationHalted):
                    await self.reservations.reserve(
                        **self.reserve_args(symbol=symbol, sid=499)
                    )
        with self.assertRaises(FourSlotReservationHalted):
            await self.reservations.reserve(
                **{k:v for k,v in self.reserve_args().items()
                   if k not in {"shared_pre_positions", "shared_pre_stops"}}
            )
        self.assertEqual(self.pg.reserved, [])

    async def test_pre_position_or_stop_drift_fails_before_sql_mutation(self):
        tampered = copy.deepcopy(self.positions)
        tampered[0]["holdVol"] = D("999")
        with self.assertRaises(FourSlotReservationHalted):
            await self.reservations.reserve(
                **{**self.reserve_args(), "shared_pre_positions": tampered}
            )
        self.assertEqual(self.pg.locks, 0)
        r = await self.reserve()
        before = self.snapshot()
        with self.assertRaises(FourSlotAttemptHalted):
            await self.journal.stage(
                **{**self.stage_args(r), "shared_pre_positions": tampered}
            )
        self.assertEqual(self.snapshot(), before)

    async def test_atomic_slot_cas_failure_rolls_back_prepared_intent(self):
        r = await self.reserve()
        before = self.snapshot()
        self.pg.fail_slot_cas = True
        with self.assertRaisesRegex(
            FourSlotAttemptHalted, "ATOMIC_ATTEMPT_LATCH_FAILED"
        ):
            await self.journal.stage(**self.stage_args(r))
        self.assertEqual(self.snapshot(), before)
        self.assertEqual(len(self.pg.new_intents), 0)

    async def test_duplicate_or_ambiguous_submission_does_not_restage(self):
        r = await self.reserve()
        await self.journal.stage(**self.stage_args(r))
        before = self.snapshot()
        with self.assertRaises(FourSlotAttemptHalted):
            await self.journal.stage(**self.stage_args(r))
        self.assertEqual(self.snapshot(), before)
        await self.journal.latch_unknown(
            signal_id=r.signal_id,
            external_oid=r.external_oid,
            lease=lease(),
        )
        self.assertEqual(self.pg.reserved[0]["state"], "UNKNOWN")
        self.assertEqual(self.pg.new_intents[0]["state"], "UNKNOWN")
        with self.assertRaises(FourSlotAttemptHalted):
            await self.journal.stage(**self.stage_args(r))
        self.assertEqual(len(self.pg.new_intents), 1)

    async def test_fencing_and_unknown_second_reservation_block(self):
        r = await self.reserve()
        before = self.snapshot()
        self.pg.lease_token = 77
        with self.assertRaises(FourSlotAttemptHalted):
            await self.journal.stage(**self.stage_args(r))
        self.assertEqual(self.snapshot(), before)
        self.pg.lease_token = lease().fencing_token
        self.pg.reserved.append({
            "signal_id": 490, "symbol": "OTHER_USDT", "state": "UNKNOWN",
        })
        before = self.snapshot()
        with self.assertRaises(FourSlotAttemptHalted):
            await self.journal.stage(**self.stage_args(r))
        self.assertEqual(self.snapshot(), before)

    async def test_full_offline_shared_reservation_stage_and_protected_db_commit(self):
        from app.t100_live_shared_protected_finalization import (
            prepare_shared_protected_entry_offline,
        )
        from app.t100_live_v40_protected_commit import (
            PostgresProtectedV40Commit,
        )
        from tests.test_t100_live_v40_protected_commit import CommitPool
        from tests.test_t100_live_shared_protected_finalization import deal

        before_manual = copy.deepcopy((self.positions, self.stops))
        reservation = await self.reserve(sid=401)
        arguments = self.stage_args(reservation)
        stage = await self.journal.stage(**arguments)
        # Simulated MEXC GET and deal-detail histories only. There is
        # intentionally NO client POST and no venue mutation from this test.
        source = self.source
        reported_order = copy.deepcopy(source.order)
        reported_order["externalOid"] = reservation.external_oid
        candidate = prepare_shared_protected_entry_offline(
            pre_witness=self.witness,
            pre_positions=self.positions,
            pre_stops=self.stops,
            stage=stage,
            intent=arguments["intent"],
            post_positions=copy.deepcopy(source.post_positions),
            post_stops=copy.deepcopy(source.post_stops),
            post_order=reported_order,
            post_inventory=copy.deepcopy(source.post_report),
            order_deals=[deal()],
        )
        for table in (
            "committed_fill_sets", "committed_fills",
            "committed_checkpoints", "committed_revisions",
        ):
            setattr(self.pg, table, {})
        self.pg.failure = None
        committing = PostgresProtectedV40Commit(CommitPool(self.pg))
        outcome = await committing.persist(
            batch=candidate, pre_attempt_witness=self.witness,
            lease=lease(), observed_at=self.observed_at,
            shared_pre_positions=self.positions,
            shared_pre_stops=self.stops,
        )
        self.assertEqual(outcome.entries_committed, 1)
        self.assertTrue(outcome.db_atomic_commit_observed)
        self.assertFalse(outcome.venue_state_currently_verified)
        self.assertFalse(outcome.live_authorization)
        self.assertEqual(self.pg.new_intents[0]["state"], "ACK")
        self.assertEqual(self.pg.reserved[0]["state"], "PROTECTED")
        self.assertEqual(len(self.pg.committed_checkpoints), 1)
        self.assertEqual(len(self.pg.committed_revisions), 1)
        self.assertEqual(len(self.pg.committed_fills), 1)
        self.assertEqual((self.positions, self.stops), before_manual)
        with self.assertRaises(Exception):
            await self.journal.stage(**arguments)
        self.assertEqual(len(self.pg.new_intents), 1)

    async def test_competing_reservations_cap_account_at_four(self):
        first = await self.reserve("NEW_USDT", 410)
        second = await self.reserve("NEXT_USDT", 411)
        self.assertEqual(
            (first.occupied_after_reservation, second.occupied_after_reservation),
            (3, 4),
        )
        with self.assertRaises(FourSlotReservationHalted):
            await self.reserve("THIRD_USDT", 412)
        self.assertEqual(len(self.pg.reserved), 2)
        # Other live reservation also blocks staging two independent POSTs
        # if one is already unresolved; no implicit batch permission.
        await self.journal.stage(**self.stage_args(first))
        with self.assertRaises(FourSlotAttemptHalted):
            await self.journal.stage(**self.stage_args(second))
        self.assertEqual(len(self.pg.new_intents), 1)


if __name__ == "__main__":
    unittest.main()
