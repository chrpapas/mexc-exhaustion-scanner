"""Offline 0–4 slot provenance/bookend fault tests. No network or SQL."""
from __future__ import annotations

import copy
import unittest
from dataclasses import replace
from decimal import Decimal as D

from app.t100_live_four_slot_planner import plan_four_slot_v40
from tests.test_t100_live_four_slot_planner import OPEN_TS, prior
from app.t100_live_four_slot_snapshot_coordinator import (
    FourSlotSnapshotCoordinator, LedgerSnapshot,
)
from tests.test_t100_live_durable_recovery import (
    checkpoint, revisions_for, intent, order, position, stop,
)


def fixture(n):
    cps, revs, intents, orders, positions, stops = [], [], [], [], [], []
    for i in range(n):
        symbol = f"S{i}_USDT"
        oid = f"t100v1e{i}"
        orderid, posid, stopid = str(100+i), str(10+i), str(90+i)
        cp = checkpoint(
            symbol=symbol, external_oid=oid, exchange_order_id=orderid,
            position_id=posid, stop_order_id=stopid,
        )
        cps.append(cp)
        revs.extend(revisions_for(cp))
        intents.append(intent(
            symbol=symbol, external_oid=oid, exchange_order_id=orderid,
        ))
        orders.append(order(
            symbol=symbol, externalOid=oid, orderId=orderid,
            positionId=posid,
        ))
        positions.append(position(symbol=symbol, positionId=posid))
        stops.append(stop(symbol=symbol, positionId=posid, id=stopid))
    return (
        LedgerSnapshot(cps, revs, intents, [], True),
        orders, positions, stops,
    )


def report(n, **changes):
    r = {
        "account_wide_completeness_verified": True,
        "critical_inventory_equal_across_witness_window": True,
        "before_positions": n,
        "after_positions": n,
        "before_current_tpsl": n,
        "after_current_tpsl": n,
        "open_order_counts": {
            "sumCount": n, "limitOrderCount": 0, "stopOrderCount": n,
            "planOrderCount": 0, "trackOrderCount": 0,
        },
    }
    r.update(changes)
    return r


class Ledger:
    def __init__(self, snap):
        self.first = copy.deepcopy(snap)
        self.last = copy.deepcopy(snap)
        self.reads = 0
        self.fail = False

    async def read_snapshot(self):
        self.reads += 1
        if self.fail:
            raise RuntimeError("DB unavailable")
        return copy.deepcopy(self.first if self.reads == 1 else self.last)


class Venue:
    def __init__(self, orders, positions, stops):
        self.orders = {
            (o["symbol"], o["externalOid"]): copy.deepcopy(o)
            for o in orders
        }
        self.first_positions = copy.deepcopy(positions)
        self.first_stops = copy.deepcopy(stops)
        self.last_positions = copy.deepcopy(positions)
        self.last_stops = copy.deepcopy(stops)
        self.positions_reads = 0
        self.stop_reads = 0
        self.order_reads = 0
        self.fail = False

    async def open_positions(self):
        self.positions_reads += 1
        if self.fail:
            raise RuntimeError("venue API unavailable")
        rows = self.first_positions if self.positions_reads == 1 else self.last_positions
        return copy.deepcopy(rows)

    async def open_stop_orders(self):
        self.stop_reads += 1
        if self.fail:
            raise RuntimeError("venue API unavailable")
        rows = self.first_stops if self.stop_reads == 1 else self.last_stops
        return copy.deepcopy(rows)

    async def order_by_external_oid(self, symbol, oid):
        self.order_reads += 1
        return copy.deepcopy(self.orders.get((symbol, oid)))


class Completeness:
    def __init__(self, r):
        self.result = copy.deepcopy(r)
        self.calls = 0
        self.fail = False

    async def inspect(self):
        self.calls += 1
        if self.fail:
            raise TimeoutError("no current certificate")
        return copy.deepcopy(self.result)


class FourSlotSnapshotTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.reset(3)

    def reset(self, n):
        snap, orders, positions, stops = fixture(n)
        self.ledger = Ledger(snap)
        self.venue = Venue(orders, positions, stops)
        self.completeness = Completeness(report(n))
        self.coordinator = FourSlotSnapshotCoordinator(
            ledger=self.ledger, venue=self.venue,
            completeness=self.completeness,
        )

    async def test_three_slots_match_order_position_stop_and_checkpoint(self):
        r = await self.coordinator.inspect()
        self.assertEqual(r.status, "NONFLAT_PROVENANCE_MATCHED_NONAUTHORIZING")
        self.assertEqual(r.occupied_slots, 3)
        self.assertEqual(len(r.recovery.recovered), 3)
        self.assertEqual(self.venue.order_reads, 3)
        self.assertEqual(self.ledger.reads, 2)
        self.assertFalse(r.entry_allowed)
        self.assertFalse(r.live_authorization)
        self.assertTrue(r.ledger_hash)
        self.assertTrue(r.critical_inventory_hash)

    async def test_three_slot_snapshot_feeds_frozen_v40_admission_without_authorization(self):
        witness = await self.coordinator.inspect()
        self.assertEqual(witness.status, "NONFLAT_PROVENANCE_MATCHED_NONAUTHORIZING")
        modeled = plan_four_slot_v40(
            inventory=witness.inventory,
            durable_recovery=witness.recovery,
            symbol="NEW_USDT", prior_bars=prior(),
            admission_open_ts=OPEN_TS, stage1_atr7_pct=6.,
            equity_usdt=10_000., marked_existing_usdt=0.,
        )
        self.assertEqual(modeled.action, "ADMIT")
        self.assertEqual(modeled.occupied_slots, 3)
        self.assertEqual(modeled.desired_notional_usdt, 6250.)
        self.assertFalse(modeled.entry_allowed)
        self.assertFalse(modeled.live_authorization)

    async def test_four_managed_slots_are_recoverable_model_only(self):
        self.reset(4)
        r = await self.coordinator.inspect()
        self.assertEqual(r.occupied_slots, 4)
        self.assertIsNotNone(r.recovery)
        self.assertFalse(r.entry_allowed)

    async def test_verified_empty_account_model_no_ledger_history(self):
        self.reset(0)
        r = await self.coordinator.inspect()
        self.assertEqual(r.status, "FLAT_OBSERVED_NONAUTHORIZING")
        self.assertEqual(r.occupied_slots, 0)
        self.assertIsNone(r.recovery)
        self.assertFalse(r.entry_allowed)

    async def test_flat_account_with_unreconciled_ack_history_blocks(self):
        self.reset(0)
        self.ledger.first.intents.append(intent())
        self.ledger.last.intents.append(intent())
        r = await self.coordinator.inspect()
        self.assertEqual(r.status, "FLAT_HISTORY_REQUIRES_CLOSED_LEDGER_AUDIT")

    async def test_existing_manual_position_with_empty_ledger_never_adopted(self):
        self.reset(0)
        self.venue.first_positions = [position()]
        self.venue.first_stops = [stop()]
        self.venue.last_positions = [position()]
        self.venue.last_stops = [stop()]
        self.completeness.result = report(1)
        r = await self.coordinator.inspect()
        self.assertEqual(r.status, "UNOWNED_OR_MISSING_CHECKPOINTS")
        self.assertEqual(self.venue.order_reads, 0)

    async def test_unrelated_fifth_position_blocks(self):
        self.reset(4)
        self.venue.first_positions.append(position(positionId="99", symbol="EXTRA_USDT"))
        r = await self.coordinator.inspect()
        self.assertEqual(r.status, "ACCOUNT_EXCEEDS_FOUR_SLOTS")

    async def test_ledger_changed_during_exchange_read_blocks(self):
        self.ledger.last.checkpoints[0]["stop_price"] = D("170")
        r = await self.coordinator.inspect()
        self.assertEqual(r.status, "LEDGER_CHANGED_DURING_OBSERVATION")
        self.assertIsNone(r.recovery)

    async def test_position_quantity_changed_during_read_blocks(self):
        self.venue.last_positions[0]["holdVol"] = D("21")
        r = await self.coordinator.inspect()
        self.assertEqual(r.status, "VENUE_CRITICAL_INVENTORY_CHANGED")

    async def test_all_owned_stops_absent_blocks_even_if_account_observation_valid(self):
        # The GET-only cross-risk witness may observe a stopless account,
        # but recovery of durable T100-owned slots MUST still fail closed.
        self.venue.first_stops = []
        self.venue.last_stops = []
        self.completeness.result["before_current_tpsl"] = 0
        self.completeness.result["after_current_tpsl"] = 0
        self.completeness.result["open_order_counts"] = {
            "sumCount": 0, "limitOrderCount": 0, "stopOrderCount": 0,
            "planOrderCount": 0, "trackOrderCount": 0,
        }
        r = await self.coordinator.inspect()
        self.assertEqual(r.status, "POSITION_STOP_COUNT_MISMATCH")
        self.assertIsNone(r.recovery)
        self.assertFalse(r.entry_allowed)
        self.assertFalse(r.live_authorization)
        self.assertEqual(self.venue.order_reads, 0)

    async def test_stop_removed_during_read_blocks(self):
        self.venue.last_stops.pop()
        r = await self.coordinator.inspect()
        self.assertEqual(r.status, "VENUE_CRITICAL_INVENTORY_CHANGED")

    async def test_stop_price_drift_from_checkpoint_blocks(self):
        self.venue.first_stops[0]["stopLossPrice"] = D("174")
        self.venue.last_stops[0]["stopLossPrice"] = D("174")
        r = await self.coordinator.inspect()
        self.assertEqual(r.status, "DURABLE_EXCHANGE_PROVENANCE_MISMATCH")

    async def test_external_order_id_drift_blocks(self):
        key = next(iter(self.venue.orders))
        self.venue.orders[key]["orderId"] = "999999"
        r = await self.coordinator.inspect()
        self.assertEqual(r.status, "DURABLE_EXCHANGE_PROVENANCE_MISMATCH")

    async def test_duplicate_stop_identity_blocks(self):
        self.venue.first_stops[1]["id"] = self.venue.first_stops[0]["id"]
        self.venue.last_stops[1]["id"] = self.venue.last_stops[0]["id"]
        r = await self.coordinator.inspect()
        self.assertEqual(r.status, "DURABLE_EXCHANGE_PROVENANCE_MISMATCH")

    async def test_missing_origin_order_blocks(self):
        self.venue.orders = {}
        r = await self.coordinator.inspect()
        self.assertEqual(r.status, "ORIGIN_ORDER_UNAVAILABLE")

    async def test_missing_completeness_blocks(self):
        self.completeness.result["account_wide_completeness_verified"] = False
        r = await self.coordinator.inspect()
        self.assertEqual(r.status, "INVENTORY_COMPLETENESS_UNVERIFIED")

    async def test_probe_count_mismatch_blocks(self):
        self.completeness.result["after_positions"] = 2
        r = await self.coordinator.inspect()
        self.assertEqual(r.status, "INVENTORY_COMPLETENESS_UNVERIFIED")

    async def test_unrelated_orders_block(self):
        counts = self.completeness.result["open_order_counts"]
        counts["limitOrderCount"] = 1
        counts["sumCount"] += 1
        r = await self.coordinator.inspect()
        self.assertEqual(r.status, "INVENTORY_COMPLETENESS_UNVERIFIED")

    async def test_malformed_stop_counts_block(self):
        self.completeness.result["open_order_counts"]["sumCount"] = True
        r = await self.coordinator.inspect()
        self.assertEqual(r.status, "INVENTORY_COMPLETENESS_UNVERIFIED")

    async def test_reader_claims_not_repeatable_read_blocks(self):
        self.ledger.first = replace(self.ledger.first, readonly_repeatable_read=False)
        r = await self.coordinator.inspect()
        self.assertEqual(r.status, "LEDGER_READ_OR_CONTRACT_FAILED")

    async def test_db_down_blocks_without_venue_calls(self):
        self.ledger.fail = True
        r = await self.coordinator.inspect()
        self.assertEqual(r.status, "LEDGER_READ_OR_CONTRACT_FAILED")
        self.assertEqual(self.venue.positions_reads, 0)

    async def test_venue_timeout_blocks(self):
        self.venue.fail = True
        self.assertEqual(
            (await self.coordinator.inspect()).status,
            "INVENTORY_READ_OR_FORMAT_FAILED",
        )

    async def test_probe_timeout_blocks(self):
        self.completeness.fail = True
        self.assertEqual(
            (await self.coordinator.inspect()).status,
            "INVENTORY_READ_OR_FORMAT_FAILED",
        )

    async def test_natural_pnl_change_does_not_break_critical_comparison(self):
        self.venue.last_positions[0]["unRealizedPnl"] = 1234
        r = await self.coordinator.inspect()
        self.assertEqual(r.status, "NONFLAT_PROVENANCE_MATCHED_NONAUTHORIZING")

    async def test_unknown_journal_state_blocks(self):
        self.ledger.first.intents[0]["state"] = "UNKNOWN"
        self.ledger.last.intents[0]["state"] = "UNKNOWN"
        r = await self.coordinator.inspect()
        self.assertEqual(r.status, "DURABLE_EXCHANGE_PROVENANCE_MISMATCH")

    async def test_checkpoint_without_expected_revision_blocks(self):
        self.ledger.first.revisions.clear()
        self.ledger.last.revisions.clear()
        r = await self.coordinator.inspect()
        self.assertEqual(r.status, "DURABLE_EXCHANGE_PROVENANCE_MISMATCH")


if __name__ == "__main__":
    unittest.main()
