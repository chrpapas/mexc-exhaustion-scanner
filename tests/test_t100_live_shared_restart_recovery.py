"""Offline simulated durable restart on one shared manual MEXC account.

All rows, including synthetic "committed" checkpoint/revision, are fixtures.
No database changes, exchange orders or Read/Write host connections.
"""
from __future__ import annotations

import copy
import unittest
from dataclasses import asdict, replace
from decimal import Decimal as D

from app.t100_live_four_slot_snapshot_coordinator import (
    FourSlotSnapshotCoordinator, LedgerSnapshot,
)
from app.t100_live_shared_restart_recovery import (
    verify_shared_restart_protected_entry,
)
from tests.test_t100_live_four_slot_snapshot_coordinator import (
    Ledger, Venue, Completeness,
)
from tests.test_t100_live_durable_recovery import revisions_for
from tests.test_t100_live_shared_protected_finalization import (
    SharedProtectedFinalizationTests,
)
from tests.test_t100_live_shared_protected_entry_observer import counts


class SharedRestartTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        source = SharedProtectedFinalizationTests()
        await source.asyncSetUp()
        self.source = source
        self.prepared = source.prepare()
        self.pre_witness = source.source.pre_witness
        self.pre_positions = copy.deepcopy(source.source.pre_positions)
        self.pre_stops = copy.deepcopy(source.source.pre_stops)
        self.after_positions = copy.deepcopy(source.source.post_positions)
        self.after_stops = copy.deepcopy(source.source.post_stops)
        self.origin = copy.deepcopy(source.order)
        entry = self.prepared.entries[0]
        cp = asdict(entry.checkpoint)
        self.snap = LedgerSnapshot(
            checkpoints=[cp],
            revisions=revisions_for(cp),
            intents=[{
                "symbol": entry.intent.symbol,
                "external_oid": entry.intent.external_oid,
                "exchange_order_id": entry.order_id,
                "volume_contracts": entry.intent.volume_contracts,
                "stop_price": entry.intent.stop_price,
                "state": "ACK",
            }],
            stop_amendments=[],
            readonly_repeatable_read=True,
        )
        self.venue = Venue([self.origin], self.after_positions, self.after_stops)
        self.ledger = Ledger(self.snap)
        self.complete = Completeness(counts(3, 1))

    async def restart(self):
        return await FourSlotSnapshotCoordinator(
            ledger=self.ledger, venue=self.venue,
            completeness=self.complete,
        ).inspect(shared_manual_observation=True)

    def check(self, witness):
        return verify_shared_restart_protected_entry(
            pre_witness=self.pre_witness,
            restart_witness=witness,
            prepared=self.prepared,
            pre_positions=self.pre_positions,
            pre_stops=self.pre_stops,
            restart_positions=self.after_positions,
            restart_stops=self.after_stops,
            origin_order=self.origin,
        )

    async def test_one_protected_owned_position_recovers_with_two_stopless_manual(self):
        witness = await self.restart()
        self.assertEqual(
            witness.status,
            "SHARED_ACCOUNT_MANAGED_PROVENANCE_MATCHED_NONAUTHORIZING",
        )
        self.assertEqual(witness.occupied_slots, 1)
        self.assertEqual(witness.manual_positions_observed, 2)
        self.assertEqual(witness.account_positions_observed, 3)
        report = self.check(witness)
        self.assertEqual(
            report.status,
            "SHARED_RESTART_EXACT_PROTECTED_PROVENANCE_OBSERVED_NONAUTHORIZING",
        )
        self.assertEqual(report.recovered_t100_positions, 1)
        self.assertEqual(report.unchanged_manual_positions, 2)
        self.assertTrue(report.owned_native_stops_recovered)
        self.assertTrue(report.new_protected_fill_record_matches_recovery)
        self.assertFalse(report.real_db_commit_independently_verified)
        self.assertFalse(report.production_restart_certified)
        self.assertFalse(report.native_stop_execution_certified)
        self.assertFalse(report.shared_cross_collateral_certified)
        self.assertFalse(report.entry_allowed)
        self.assertFalse(report.live_authorization)
        self.assertFalse(report.mexc_orders_sent)
        self.assertEqual(self.ledger.reads, 2)
        self.assertEqual(self.venue.positions_reads, 2)
        self.assertEqual(self.venue.stop_reads, 2)
        self.assertEqual(self.venue.order_reads, 1)

    async def test_restart_with_ledger_missing_checkpoint_does_not_adopt_manual(self):
        self.ledger.first.checkpoints.clear()
        self.ledger.last.checkpoints.clear()
        self.ledger.first.revisions.clear()
        self.ledger.last.revisions.clear()
        self.ledger.first.intents.clear()
        self.ledger.last.intents.clear()
        witness = await self.restart()
        self.assertNotEqual(
            witness.status,
            "SHARED_ACCOUNT_MANAGED_PROVENANCE_MATCHED_NONAUTHORIZING",
        )
        self.assertFalse(self.check(witness).entry_allowed)

    async def test_restart_missing_revision_fails_closed(self):
        self.ledger.first.revisions.clear()
        self.ledger.last.revisions.clear()
        witness = await self.restart()
        self.assertEqual(
            witness.status,
            "SHARED_ACCOUNT_T100_DURABLE_PROVENANCE_UNVERIFIED",
        )
        self.assertFalse(self.check(witness).owned_native_stops_recovered)

    async def test_restart_unresolved_intent_fails_closed(self):
        self.ledger.first.intents[0]["state"] = "UNKNOWN"
        self.ledger.last.intents[0]["state"] = "UNKNOWN"
        witness = await self.restart()
        self.assertEqual(
            witness.status,
            "SHARED_ACCOUNT_T100_DURABLE_PROVENANCE_UNVERIFIED",
        )
        self.assertFalse(self.check(witness).entry_allowed)

    async def test_restart_new_native_stop_missing_fails_closed(self):
        self.venue.first_stops.clear()
        self.venue.last_stops.clear()
        self.complete.result = counts(3, 0)
        witness = await self.restart()
        self.assertNotEqual(
            witness.status,
            "SHARED_ACCOUNT_MANAGED_PROVENANCE_MATCHED_NONAUTHORIZING",
        )
        self.assertFalse(self.check(witness).owned_native_stops_recovered)

    async def test_restart_wrong_origin_order_id_fails_closed(self):
        only = next(iter(self.venue.orders))
        self.venue.orders[only]["orderId"] = "901"
        witness = await self.restart()
        self.assertEqual(
            witness.status,
            "SHARED_ACCOUNT_T100_DURABLE_PROVENANCE_UNVERIFIED",
        )
        self.assertFalse(self.check(witness).entry_allowed)

    async def test_manual_position_change_after_entry_blocks_recovery_witness(self):
        self.after_positions[1]["holdVol"] = D("21")
        self.venue.first_positions[1]["holdVol"] = D("21")
        self.venue.last_positions[1]["holdVol"] = D("21")
        witness = await self.restart()
        self.assertEqual(
            witness.status,
            "SHARED_ACCOUNT_MANAGED_PROVENANCE_MATCHED_NONAUTHORIZING",
        )
        out = self.check(witness)
        self.assertEqual(
            out.status, "SHARED_RESTART_EXCHANGE_OR_MANUAL_INVENTORY_DRIFT"
        )

    async def test_recovery_persisted_stop_identity_mismatch_blocks(self):
        for snapshot in (self.ledger.first, self.ledger.last):
            snapshot.checkpoints[0]["stop_order_id"] = "999999"
            snapshot.revisions[0]["stop_order_id"] = "999999"
        witness = await self.restart()
        self.assertNotEqual(
            witness.status,
            "SHARED_ACCOUNT_MANAGED_PROVENANCE_MATCHED_NONAUTHORIZING",
        )
        self.assertFalse(self.check(witness).entry_allowed)

    async def test_forged_or_changed_prepared_checkpoint_blocks(self):
        witness = await self.restart()
        x = self.prepared.entries[0]
        forged = replace(self.prepared, entries=(
            replace(x, checkpoint=replace(
                x.checkpoint, stop_order_id="fabricated",
            )),
        ))
        result = verify_shared_restart_protected_entry(
            pre_witness=self.pre_witness,
            restart_witness=witness,
            prepared=forged,
            pre_positions=self.pre_positions,
            pre_stops=self.pre_stops,
            restart_positions=self.after_positions,
            restart_stops=self.after_stops,
            origin_order=self.origin,
        )
        self.assertEqual(
            result.status, "SHARED_RESTART_T100_CHECKPOINT_OR_STOP_MISMATCH"
        )
        self.assertFalse(result.live_authorization)

    async def test_failure_on_nonrepeatable_ledger_and_venue_bookend_drift(self):
        self.ledger.first = replace(
            self.ledger.first, readonly_repeatable_read=False,
        )
        witness = await self.restart()
        self.assertEqual(
            witness.status, "LEDGER_READ_OR_CONTRACT_FAILED",
        )
        self.assertFalse(self.check(witness).entry_allowed)
        self.ledger.first = copy.deepcopy(self.snap)
        self.ledger.last = copy.deepcopy(self.snap)
        self.venue.last_positions[-1]["holdVol"] = D("240")
        witness = await self.restart()
        self.assertEqual(
            witness.status, "VENUE_CRITICAL_INVENTORY_CHANGED",
        )
        self.assertFalse(self.check(witness).entry_allowed)

    def test_never_uses_mexc_post_or_db_or_trading_permits(self):
        import inspect
        import app.t100_live_shared_restart_recovery as m
        code = inspect.getsource(m)
        for dangerous in (
            "._private_post(", ".send_one(",
            "MexcTradeClient(", "asyncpg.", "LiveMutationPermit",
            "submit_short_with_stop(",
        ):
            self.assertNotIn(dangerous, code)


if __name__ == "__main__":
    unittest.main()
