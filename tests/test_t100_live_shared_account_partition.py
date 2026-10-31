"""Shared CROSS account bookended GET/SELECT observation regressions.

No live order, DB write, or wallet account access is possible in these tests.
Two manual positions can lack native stops; a T100-owned position cannot.
"""
from __future__ import annotations

import copy
import unittest
from decimal import Decimal as D

from app.t100_live_four_slot_readonly_cli import run_readonly_four_slot_audit
from app.t100_live_shared_account_partition import (
    SharedPartitionHalted, partition_shared_account,
)
from app.t100_live_durable_recovery import checkpoint_from_row
from tests.test_t100_live_four_slot_mexc_readonly import (
    MockMexcGetOnly, prepare_rows,
)
from tests.test_t100_live_four_slot_snapshot_coordinator import Ledger


SAFE_ENV = {
    "T100_EXECUTION_MODE": "paper",
    "T100_LIVE_ORDER_API_ENABLED": "false",
    "T100_LIVE_KILL_SWITCH": "true",
}


async def audit(snap, orders, positions, stops, *, shared=True):
    client = MockMexcGetOnly(orders, positions, stops)
    result = await run_readonly_four_slot_audit(
        ledger=Ledger(snap), client=client, env=SAFE_ENV,
        shared_manual_observation=shared,
    )
    return result, client


class SharedAccountPartitionTests(unittest.IsolatedAsyncioTestCase):
    async def test_two_manual_positions_with_only_tp_only_stop_are_observed(self):
        snap, _, positions, stops = prepare_rows(2)
        empty_snap, _, _, _ = prepare_rows(0)
        stops[0]["stopLossPrice"] = "0"
        result, client = await audit(
            empty_snap, [], positions, [stops[0]],
        )
        self.assertEqual(
            result["status"], "SHARED_ACCOUNT_MANUAL_ONLY_NONAUTHORIZING",
        )
        self.assertEqual(result["owned_four_slot_positions_observed"], 0)
        self.assertEqual(result["manual_positions_observed"], 2)
        self.assertEqual(result["account_positions_observed"], 2)
        self.assertEqual(
            result["manual_positions_without_positive_sl_observed"], 2,
        )
        self.assertFalse(result["manual_positions_adopted_by_t100"])
        self.assertFalse(result["cross_account_risk_certified"])
        self.assertFalse(result["entry_allowed"])
        self.assertFalse(result["live_authorization"])
        self.assertFalse(result["mexc_orders_sent"])
        self.assertFalse(result["database_writes_sent"])
        self.assertTrue(all("/private/" in path for path, _ in client.calls))
        # Existing strict four-slot recovery keeps its original policy.
        strict, _ = await audit(
            empty_snap, [], positions, [stops[0]], shared=False,
        )
        self.assertEqual(strict["status"], "POSITION_STOP_COUNT_MISMATCH")

    async def test_two_stopless_manual_positions_are_not_required_to_have_sl(self):
        empty_snap, _, _, _ = prepare_rows(0)
        _, _, positions, _ = prepare_rows(2)
        result, _ = await audit(empty_snap, [], positions, [])
        self.assertEqual(
            result["status"], "SHARED_ACCOUNT_MANUAL_ONLY_NONAUTHORIZING",
        )
        self.assertEqual(
            result["manual_positions_without_positive_sl_observed"], 2,
        )
        self.assertFalse(result["live_authorization"])

    async def test_owned_native_stop_is_still_required_among_two_manual_positions(self):
        snap, orders, positions, stops = prepare_rows(3)
        owned = copy.deepcopy(snap)
        owned.checkpoints = owned.checkpoints[:1]
        owned.revisions = owned.revisions[:1]
        owned.intents = owned.intents[:1]
        positions[1]["openType"] = 2
        positions[2]["openType"] = 2
        stops[1]["stopLossPrice"] = "0"
        good, client = await audit(
            owned, orders[:1], positions, [stops[0], stops[1]],
        )
        self.assertEqual(
            good["status"],
            "SHARED_ACCOUNT_MANAGED_PROVENANCE_MATCHED_NONAUTHORIZING",
        )
        self.assertEqual(good["owned_four_slot_positions_observed"], 1)
        self.assertEqual(good["manual_positions_observed"], 2)
        self.assertEqual(good["account_positions_observed"], 3)
        self.assertEqual(
            good["manual_positions_without_positive_sl_observed"], 2,
        )
        self.assertFalse(good["cross_account_risk_certified"])
        self.assertFalse(good["entry_allowed"])
        self.assertFalse(good["live_authorization"])
        self.assertEqual(
            sum("/order/external/" in path for path, _ in client.calls), 1,
        )

        # Remove T100's own native stop, leaving the manual TP-only row.
        bad, _ = await audit(
            owned, orders[:1], positions, [stops[1]],
        )
        self.assertEqual(
            bad["status"], "SHARED_ACCOUNT_POSITION_PARTITION_UNVERIFIED",
        )
        self.assertIsNone(bad["owned_four_slot_positions_observed"])
        self.assertFalse(bad["entry_allowed"])

    async def test_t100_owned_stop_wrong_price_rejects_provenance(self):
        snap, orders, positions, stops = prepare_rows(3)
        owned = copy.deepcopy(snap)
        owned.checkpoints = owned.checkpoints[:1]
        owned.revisions = owned.revisions[:1]
        owned.intents = owned.intents[:1]
        stops[0]["stopLossPrice"] = D("174")
        result, _ = await audit(
            owned, orders[:1], positions, [stops[0]],
        )
        self.assertEqual(
            result["status"],
            "SHARED_ACCOUNT_T100_DURABLE_PROVENANCE_UNVERIFIED",
        )
        self.assertFalse(result["live_authorization"])

    async def test_manual_position_same_symbol_as_owned_is_blocked(self):
        snap, orders, positions, stops = prepare_rows(1)
        manual = copy.deepcopy(positions[0])
        manual["positionId"] = "manual-standalone-position"
        result, _ = await audit(
            snap, orders, [*positions, manual], stops,
        )
        self.assertEqual(
            result["status"], "SHARED_ACCOUNT_POSITION_PARTITION_UNVERIFIED",
        )

    async def test_unmatched_or_duplicate_manual_stop_is_blocked(self):
        empty_snap, _, _, _ = prepare_rows(0)
        _, _, positions, stops = prepare_rows(2)
        orphan = dict(stops[0], positionId="unknown-person")
        bad, _ = await audit(empty_snap, [], positions, [orphan])
        self.assertEqual(
            bad["status"], "SHARED_ACCOUNT_POSITION_PARTITION_UNVERIFIED",
        )
        duplicate, _ = await audit(
            empty_snap, [], positions, [stops[0], stops[0]],
        )
        self.assertEqual(
            duplicate["status"], "SHARED_ACCOUNT_POSITION_PARTITION_UNVERIFIED",
        )

    async def test_bookend_change_or_unrelated_pending_order_stays_blocked(self):
        # Using pure coordinator mocks to simulate change during GET window
        from app.t100_live_four_slot_snapshot_coordinator import (
            FourSlotSnapshotCoordinator,
        )
        from tests.test_t100_live_four_slot_snapshot_coordinator import (
            Completeness, Venue, report, fixture,
        )
        empty_snap, _, _, _ = prepare_rows(0)
        _, _, positions, stops = fixture(2)
        venue = Venue([], positions, stops[:1])
        completeness = report(2)
        completeness["before_current_tpsl"] = 1
        completeness["after_current_tpsl"] = 1
        completeness["open_order_counts"] = {
            "sumCount": 1, "limitOrderCount": 0,
            "stopOrderCount": 1, "planOrderCount": 0,
            "trackOrderCount": 0,
        }
        coord = FourSlotSnapshotCoordinator(
            ledger=Ledger(empty_snap),
            venue=venue,
            completeness=Completeness(completeness),
        )
        venue.last_positions[0]["holdVol"] = D("17")
        changed = await coord.inspect(shared_manual_observation=True)
        self.assertEqual(changed.status, "VENUE_CRITICAL_INVENTORY_CHANGED")
        self.assertFalse(changed.live_authorization)
        # Verify unrelated pending orders are not declared safe by shared
        # handling even if the count has a consistent sum.
        venue.last_positions = copy.deepcopy(positions)
        counts = completeness["open_order_counts"]
        counts["limitOrderCount"] = 1
        counts["sumCount"] = 2
        coord = FourSlotSnapshotCoordinator(
            ledger=Ledger(empty_snap), venue=Venue([], positions, stops[:1]),
            completeness=Completeness(completeness),
        )
        pending = await coord.inspect(shared_manual_observation=True)
        self.assertEqual(pending.status, "INVENTORY_COMPLETENESS_UNVERIFIED")

    def test_partition_rejects_duplicate_owned_checkpoint_identity(self):
        snap, _, positions, stops = prepare_rows(1)
        cp = checkpoint_from_row(snap.checkpoints[0])
        with self.assertRaises(SharedPartitionHalted):
            partition_shared_account(
                checkpoints=[cp, cp],
                positions=positions, stops=stops,
            )

    async def test_redacted_shared_observation_never_prints_venue_data(self):
        empty_snap, _, _, _ = prepare_rows(0)
        _, _, positions, stops = prepare_rows(2)
        result, _ = await audit(empty_snap, [], positions, [stops[0]])
        rendered = str(result)
        for token in (
            positions[0]["symbol"], positions[0]["positionId"],
            positions[1]["symbol"], str(stops[0]["id"]),
        ):
            self.assertNotIn(str(token), rendered)


if __name__ == "__main__":
    unittest.main()
