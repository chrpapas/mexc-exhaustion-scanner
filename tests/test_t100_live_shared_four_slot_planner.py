"""Owner-aware shared-account V40 planning; all checks purely offline.

Uses the actual shared GET/SELECT coordinator witness, not a bool or
manual label. Historic frozen strategy is only modeled; NO POST/permitted
live trading is possible. Two stopless manual trades still occupy 2 slots.
"""
from __future__ import annotations

import copy
import unittest
from dataclasses import replace

from app.t100_live_four_slot_planner import (
    plan_four_slot_v40, plan_shared_four_slot_v40,
)
from app.t100_live_four_slot_snapshot_coordinator import (
    FourSlotSnapshotCoordinator,
)
from tests.test_t100_live_four_slot_planner import (
    OPEN_TS, inventory, prior, recovery,
)
from tests.test_t100_live_four_slot_snapshot_coordinator import (
    Completeness, Ledger, Venue, fixture, report,
)


def manual_two():
    empty, _, _, _ = fixture(0)
    _, _, positions, _ = fixture(2)
    positions[0]["symbol"] = "MANUALA_USDT"
    positions[0]["openType"] = 2
    positions[0]["positionType"] = 2
    positions[1]["symbol"] = "MANUALB_USDT"
    positions[1]["openType"] = 1
    positions[1]["positionType"] = 1
    return empty, positions


def planner_args(snapshot, positions, stops, **changes):
    kwargs = {
        "snapshot": snapshot,
        "account_positions": positions,
        "account_stops": stops,
        "symbol": "NEW_USDT",
        "prior_bars": prior(),
        "admission_open_ts": OPEN_TS,
        "stage1_atr7_pct": 6.0,
        "equity_usdt": 10000.,
        "marked_existing_usdt": 1000.,
    }
    kwargs.update(changes)
    return plan_shared_four_slot_v40(**kwargs)


def no_manual_stops_report(n):
    r = report(n)
    r["before_current_tpsl"] = 0
    r["after_current_tpsl"] = 0
    r["open_order_counts"] = {
        "sumCount": 0, "limitOrderCount": 0,
        "stopOrderCount": 0, "planOrderCount": 0,
        "trackOrderCount": 0,
    }
    return r


class SharedFourSlotPlannerTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.empty, self.positions = manual_two()
        self.ledger = Ledger(self.empty)
        self.venue = Venue([], self.positions, [])
        self.completeness = Completeness(no_manual_stops_report(2))
        self.coordinator = FourSlotSnapshotCoordinator(
            ledger=self.ledger,
            venue=self.venue,
            completeness=self.completeness,
        )
        self.snapshot = await self.coordinator.inspect(
            shared_manual_observation=True,
        )
        self.assertEqual(
            self.snapshot.status, "SHARED_ACCOUNT_MANUAL_ONLY_NONAUTHORIZING"
        )
        self.assertFalse(self.snapshot.entry_allowed)

    def test_two_stopless_manual_positions_consume_two_four_slots(self):
        p = planner_args(self.snapshot, self.positions, [])
        self.assertEqual(p.action, "ADMIT")
        self.assertEqual(p.reason, "modeled_single_bar_admission")
        self.assertEqual(p.occupied_slots, 2)
        self.assertEqual(p.desired_notional_usdt, 6250.)
        self.assertGreater(p.modeled_notional_usdt, 0.)
        self.assertFalse(p.entry_allowed)
        self.assertFalse(p.live_authorization)

    def test_both_manual_symbol_conflicts_are_rejected(self):
        for symbol in ("MANUALA_USDT", "MANUALB_USDT"):
            with self.subTest(symbol=symbol):
                p = planner_args(
                    self.snapshot, self.positions, [], symbol=symbol,
                )
                self.assertEqual((p.action, p.reason), (
                    "REJECT", "duplicate_symbol",
                ))
                self.assertEqual(p.occupied_slots, 2)

    def test_slot_occupancy_preserves_low_tier_and_volatility_skip(self):
        low = planner_args(
            self.snapshot, self.positions, [], stage1_atr7_pct=5.,
        )
        self.assertEqual(low.action, "ADMIT")
        self.assertEqual(low.desired_notional_usdt, 2500.)
        missing = planner_args(
            self.snapshot, self.positions, [], prior_bars=[],
        )
        self.assertEqual(
            (missing.action, missing.reason), (
                "MISSING", "causal_49_bar_history_incomplete"
            ),
        )
        below = planner_args(
            self.snapshot, self.positions, [], stage1_atr7_pct=3.9,
        )
        self.assertEqual(below.reason, "below_ATR4_candidate_floor")

    def test_stale_or_forged_inventory_hash_blocks(self):
        altered = copy.deepcopy(self.positions)
        altered[0]["holdVol"] = "999"
        res = planner_args(self.snapshot, altered, [])
        self.assertEqual(res.reason, "SHARED_ACCOUNT_INVENTORY_HASH_DRIFT")
        swapped = copy.deepcopy(self.positions)
        swapped[0]["symbol"] = "DIFFERENT_USDT"
        self.assertEqual(
            planner_args(self.snapshot, swapped, []).reason,
            "SHARED_ACCOUNT_INVENTORY_HASH_DRIFT",
        )
        self.assertEqual(
            planner_args(
                replace(self.snapshot, critical_inventory_hash="0" * 64),
                self.positions, [],
            ).reason,
            "SHARED_ACCOUNT_INVENTORY_HASH_DRIFT",
        )

    def test_legacy_nonshared_witness_not_accepted_as_shared(self):
        fake = replace(
            self.snapshot, status="NONFLAT_PROVENANCE_MATCHED_NONAUTHORIZING",
        )
        p = planner_args(fake, self.positions, [])
        self.assertEqual(p.reason, "SHARED_ACCOUNT_SNAPSHOT_STATUS_UNVERIFIED")

    def test_unrelated_pending_orders_fail_closed(self):
        report_copy = copy.deepcopy(self.snapshot.inventory)
        report_copy["open_order_counts"]["limitOrderCount"] = 1
        report_copy["open_order_counts"]["sumCount"] = 1
        p = planner_args(
            replace(self.snapshot, inventory=report_copy),
            self.positions, [],
        )
        self.assertEqual(p.reason, "SHARED_ACCOUNT_INVENTORY_COUNTS_UNVERIFIED")

    def test_missing_completeness_or_false_certificate_blocks(self):
        for field in (
            "account_wide_completeness_verified",
            "critical_inventory_equal_across_witness_window",
        ):
            with self.subTest(field=field):
                rep = copy.deepcopy(self.snapshot.inventory)
                rep[field] = False
                self.assertEqual(
                    planner_args(
                        replace(self.snapshot, inventory=rep),
                        self.positions, [],
                    ).action,
                    "BLOCK",
                )

    def test_snapshot_count_and_missing_checkpoint_recovery_block(self):
        p = planner_args(
            replace(self.snapshot, manual_positions_observed=1),
            self.positions, [],
        )
        self.assertEqual(p.reason, "SHARED_ACCOUNT_SLOT_COUNTS_UNVERIFIED")
        fake = replace(
            self.snapshot, occupied_slots=1, manual_positions_observed=1,
        )
        self.assertEqual(
            planner_args(fake, self.positions, []).action, "BLOCK"
        )

    def test_malformed_position_mode_or_duplicate_symbol_blocks(self):
        p = copy.deepcopy(self.positions)
        p[1]["openType"] = 3
        # This is also different from the witness: first preserve the
        # observed hash on purpose to exercise identity validation.
        from app.t100_live_four_slot_snapshot_coordinator import _venue_digest
        snap = replace(
            self.snapshot, critical_inventory_hash=_venue_digest(p, []),
        )
        self.assertEqual(
            planner_args(snap, p, []).reason,
            "SHARED_ACCOUNT_POSITION_IDENTITY_UNVERIFIED",
        )
        duplicate = copy.deepcopy(self.positions)
        duplicate[1]["symbol"] = duplicate[0]["symbol"]
        snap2 = replace(
            self.snapshot, critical_inventory_hash=_venue_digest(duplicate, []),
        )
        self.assertEqual(
            planner_args(snap2, duplicate, []).reason,
            "SHARED_ACCOUNT_POSITION_IDENTITY_UNVERIFIED",
        )

    async def test_one_recovered_t100_and_two_manual_preserve_last_slot(self):
        snap, orders, positions, stops = fixture(3)
        owned = replace(
            snap, checkpoints=snap.checkpoints[:1],
            revisions=snap.revisions[:1], intents=snap.intents[:1],
        )
        positions[1]["openType"] = 2
        positions[2]["openType"] = 1
        positions[2]["positionType"] = 1
        positions[1]["symbol"] = "MANUALA_USDT"
        positions[2]["symbol"] = "MANUALB_USDT"
        protected = stops[:1]
        completeness = report(3)
        completeness["before_current_tpsl"] = 1
        completeness["after_current_tpsl"] = 1
        completeness["open_order_counts"] = {
            "sumCount": 1, "limitOrderCount": 0,
            "stopOrderCount": 1, "planOrderCount": 0,
            "trackOrderCount": 0,
        }
        witness = await FourSlotSnapshotCoordinator(
            ledger=Ledger(owned),
            venue=Venue(orders[:1], positions, protected),
            completeness=Completeness(completeness),
        ).inspect(shared_manual_observation=True)
        self.assertEqual(
            witness.status,
            "SHARED_ACCOUNT_MANAGED_PROVENANCE_MATCHED_NONAUTHORIZING",
        )
        p = planner_args(witness, positions, protected)
        self.assertEqual((p.action, p.occupied_slots), ("ADMIT", 3))
        self.assertFalse(p.entry_allowed)
        self.assertFalse(p.live_authorization)
        self.assertEqual(
            planner_args(witness, positions, protected, symbol="S0_USDT").reason,
            "duplicate_symbol",
        )
        self.assertEqual(
            planner_args(
                witness, positions, protected, symbol="MANUALB_USDT",
            ).reason,
            "duplicate_symbol",
        )
        self.assertEqual(
            planner_args(witness, positions, []).action,
            "BLOCK",
        )

    async def test_four_manual_positions_reject_entry_even_without_any_stops(self):
        empty, _, _, _ = fixture(0)
        _, _, positions, _ = fixture(4)
        for i, p in enumerate(positions):
            p["symbol"] = f"MANUAL{i}_USDT"
            p["openType"] = 1 if i % 2 else 2
            p["positionType"] = 1 if i % 2 else 2
        witness = await FourSlotSnapshotCoordinator(
            ledger=Ledger(empty),
            venue=Venue([], positions, []),
            completeness=Completeness(no_manual_stops_report(4)),
        ).inspect(shared_manual_observation=True)
        self.assertEqual(
            witness.status, "SHARED_ACCOUNT_MANUAL_ONLY_NONAUTHORIZING",
        )
        p = planner_args(witness, positions, [])
        self.assertEqual((p.action, p.reason), ("REJECT", "four_slot_limit"))
        self.assertEqual(p.occupied_slots, 4)
        self.assertFalse(p.live_authorization)

    def test_legacy_flat_planner_policy_is_unchanged(self):
        old = plan_four_slot_v40(
            inventory=inventory(0), durable_recovery=None,
            symbol="NEW_USDT", prior_bars=prior(),
            admission_open_ts=OPEN_TS, stage1_atr7_pct=6.,
            equity_usdt=10000., marked_existing_usdt=0.,
        )
        self.assertEqual(old.action, "ADMIT")
        self.assertEqual(old.desired_notional_usdt, 6250.)
        self.assertFalse(old.live_authorization)


if __name__ == "__main__":
    unittest.main()
