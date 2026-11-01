"""Offline shared-account protected entry lifecycle regression, NO MEXC POST.

Models exchange-supplied GET results only, including ambiguous external order,
partial fill, stopless manual positions, native SL volume and bookend drift.
"""
from __future__ import annotations

import copy
import unittest
from decimal import Decimal as D

from app.t100_live_execution import EntryIntent
from app.t100_live_four_slot_attempt_journal import DurableAttemptStage
from app.t100_live_four_slot_snapshot_coordinator import FourSlotSnapshotCoordinator
from app.t100_live_shared_protected_entry_observer import (
    assess_shared_protected_entry, observe_shared_protected_entry,
)
from tests.test_t100_live_four_slot_snapshot_coordinator import (
    fixture, Ledger, Venue, Completeness, report,
)


def counts(npositions, nstops, *, limit=0):
    r = report(npositions)
    r.update(before_current_tpsl=nstops, after_current_tpsl=nstops)
    r["open_order_counts"] = {
        "sumCount": nstops + limit,
        "limitOrderCount": limit,
        "stopOrderCount": nstops,
        "planOrderCount": 0,
        "trackOrderCount": 0,
    }
    return r


class SharedProtectedEntryTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        empty, _, pre, _ = fixture(2)
        empty.checkpoints.clear()
        empty.revisions.clear()
        empty.intents.clear()
        self.pre_positions = pre
        self.pre_positions[0].update(symbol="MANUALA_USDT", openType=2)
        self.pre_positions[1].update(
            symbol="MANUALB_USDT", openType=1, positionType=1,
        )
        self.pre_stops = []
        self.pre_complete = Completeness(counts(2, 0))
        self.pre_witness = await FourSlotSnapshotCoordinator(
            ledger=Ledger(empty),
            venue=Venue([], self.pre_positions, self.pre_stops),
            completeness=self.pre_complete,
        ).inspect(shared_manual_observation=True)
        self.assertEqual(
            self.pre_witness.status,
            "SHARED_ACCOUNT_MANUAL_ONLY_NONAUTHORIZING",
        )
        self.intent = EntryIntent.build(
            signal_id=401,
            symbol="NEW_USDT",
            reference_price="2",
            notional_usdt="500",
            contract_size="1", volume_step="1", min_volume="1",
            max_volume="1000", price_tick="0.01",
        )
        self.stage = DurableAttemptStage(
            signal_id=self.intent.signal_id,
            external_oid=self.intent.external_oid,
        )
        self.order = {
            "symbol": "NEW_USDT", "externalOid": self.intent.external_oid,
            "orderId": "900", "positionId": "390",
            "side": 3, "state": 3,
            "dealVol": D("250"), "dealAvgPrice": D("2"),
        }
        self.post_positions = [*copy.deepcopy(self.pre_positions), {
            "symbol": "NEW_USDT", "positionId": "390",
            "positionType": 2, "openType": 2, "state": 1,
            "holdVol": D("250"), "holdAvgPrice": D("2"),
        }]
        self.post_stops = [{
            "symbol": "NEW_USDT", "positionId": "390",
            "id": "930", "positionType": 2,
            "state": 1, "isFinished": 0,
            "stopLossPrice": D("3.5"), "vol": D("250"),
        }]
        self.post_report = counts(3, 1)
        self.venue = Venue(
            [self.order], self.post_positions, self.post_stops,
        )
        self.complete = Completeness(self.post_report)

    async def observed(self):
        return await observe_shared_protected_entry(
            pre_witness=self.pre_witness,
            pre_positions=self.pre_positions,
            pre_stops=self.pre_stops,
            stage=self.stage, intent=self.intent,
            venue=self.venue, completeness=self.complete,
        )

    async def test_protected_full_fill_two_stopless_manual_positions(self):
        outcome = await self.observed()
        self.assertEqual(
            outcome.status,
            "SHARED_PROTECTED_SINGLE_ENTRY_OBSERVED_NONAUTHORIZING",
        )
        self.assertEqual(outcome.preexisting_manual_positions, 2)
        self.assertEqual(outcome.preexisting_owned_positions, 0)
        self.assertEqual(outcome.total_positions_after, 3)
        self.assertTrue(outcome.new_t100_full_fill_and_native_stop_observed)
        self.assertTrue(outcome.legacy_manual_inventory_preserved)
        self.assertFalse(outcome.journal_ack_persisted)
        self.assertFalse(outcome.durable_checkpoint_persisted)
        self.assertFalse(outcome.native_stop_execution_certified)
        self.assertFalse(outcome.entry_allowed)
        self.assertFalse(outcome.live_authorization)
        self.assertFalse(outcome.mexc_orders_sent)
        self.assertEqual(self.venue.positions_reads, 2)
        self.assertEqual(self.venue.stop_reads, 2)
        self.assertEqual(self.venue.order_reads, 2)

    async def test_partial_or_nonfinal_fill_stays_unresolved(self):
        for change in ({"dealVol": D("249")}, {"state": 2}):
            with self.subTest(change=change):
                r = assess_shared_protected_entry(
                    pre_witness=self.pre_witness,
                    pre_positions=self.pre_positions, pre_stops=[],
                    stage=self.stage, intent=self.intent,
                    post_positions=self.post_positions,
                    post_stops=self.post_stops,
                    post_order={**self.order, **change},
                    post_inventory=self.post_report,
                )
                self.assertEqual(
                    r.status, "SHARED_PROTECTED_FILL_OR_POSITION_NOT_FINAL"
                )
                self.assertFalse(r.live_authorization)

    async def test_native_stop_missing_and_undervolume_block(self):
        r = assess_shared_protected_entry(
            pre_witness=self.pre_witness,
            pre_positions=self.pre_positions, pre_stops=[],
            stage=self.stage, intent=self.intent,
            post_positions=self.post_positions, post_stops=[],
            post_order=self.order, post_inventory=counts(3, 0),
        )
        self.assertEqual(
            r.status, "SHARED_PROTECTED_NATIVE_STOP_MISSING_OR_DUPLICATE"
        )
        for change in (
            {"vol": D("249")}, {"stopLossPrice": D("3.51")},
            {"state": 2}, {"isFinished": 1},
            {"positionType": 1},
        ):
            with self.subTest(change=change):
                rows = [{**self.post_stops[0], **change}]
                self.venue.first_stops = copy.deepcopy(rows)
                self.venue.last_stops = copy.deepcopy(rows)
                r = await self.observed()
                self.assertEqual(
                    r.status, "SHARED_PROTECTED_NATIVE_STOP_INVALID_OR_UNDERCOVERED"
                )
                self.assertFalse(r.live_authorization)

    async def test_manual_inventory_mutation_blocks_even_if_t100_stop_exists(self):
        for change in (
            {"holdVol": D("21")}, {"positionType": 1},
            {"openType": 1}, {"holdAvgPrice": D("101")},
        ):
            with self.subTest(change=change):
                altered = copy.deepcopy(self.post_positions)
                altered[0].update(change)
                self.venue.first_positions = copy.deepcopy(altered)
                self.venue.last_positions = copy.deepcopy(altered)
                out = await self.observed()
                self.assertEqual(
                    out.status, "SHARED_PROTECTED_PREEXISTING_ACCOUNT_DRIFT"
                )
                self.assertFalse(out.legacy_manual_inventory_preserved)

    async def test_new_order_identity_and_cross_side_required(self):
        cases = (
            ("order", {"externalOid": "NOT_THE_SUBMITTED_OID"}),
            ("order", {"side": 1}),
            ("new", {"openType": 1}),
            ("new", {"positionType": 1}),
            ("new", {"holdVol": D("249")}),
        )
        for type_, change in cases:
            with self.subTest(type=type_, change=change):
                order = dict(self.order)
                positions = copy.deepcopy(self.post_positions)
                if type_ == "order":
                    order.update(change)
                else:
                    positions[-1].update(change)
                out = assess_shared_protected_entry(
                    pre_witness=self.pre_witness,
                    pre_positions=self.pre_positions, pre_stops=[],
                    stage=self.stage, intent=self.intent,
                    post_positions=positions,
                    post_stops=self.post_stops,
                    post_order=order, post_inventory=self.post_report,
                )
                self.assertEqual(
                    out.status, "SHARED_PROTECTED_FILL_OR_POSITION_NOT_FINAL"
                )

    async def test_external_oid_unknown_does_not_retry_or_certify(self):
        self.venue.orders.clear()
        out = await self.observed()
        self.assertEqual(
            out.status, "SHARED_PROTECTED_ORDER_OR_GET_UNRESOLVED_NO_RETRY"
        )
        self.assertFalse(out.mexc_orders_sent)
        self.assertFalse(out.durable_checkpoint_persisted)

    async def test_post_bookend_change_blocks(self):
        self.venue.last_positions[-1]["holdVol"] = D("249")
        r = await self.observed()
        self.assertEqual(r.status, "SHARED_PROTECTED_POST_BOOKEND_DRIFT")

    async def test_unrelated_open_orders_block(self):
        self.complete.result = counts(3, 1, limit=1)
        r = await self.observed()
        self.assertEqual(
            r.status, "SHARED_PROTECTED_POST_COMPLETENESS_OR_ORDERS_UNVERIFIED"
        )

    async def test_implicit_manual_stop_and_its_identity_preserved(self):
        # Manual TP-only is allowed if exactly unchanged across observations.
        manual_tp = {
            "symbol": "MANUALB_USDT", "positionId": "11",
            "id": "901", "takeProfitPrice": D("111"),
            "stopLossPrice": D("0"),
        }
        # Position IDs from fixture are 10 and 11.
        before_stops = [manual_tp]
        prior, *_ = fixture(0)
        from app.t100_live_four_slot_snapshot_coordinator import (
            FourSlotSnapshotCoordinator,
        )
        witness = await FourSlotSnapshotCoordinator(
            ledger=Ledger(prior),
            venue=Venue([], self.pre_positions, before_stops),
            completeness=Completeness(counts(2, 1)),
        ).inspect(shared_manual_observation=True)
        self.assertEqual(
            witness.status, "SHARED_ACCOUNT_MANUAL_ONLY_NONAUTHORIZING",
        )
        out = assess_shared_protected_entry(
            pre_witness=witness,
            pre_positions=self.pre_positions, pre_stops=before_stops,
            stage=self.stage, intent=self.intent,
            post_positions=self.post_positions,
            post_stops=[manual_tp, *self.post_stops],
            post_order=self.order, post_inventory=counts(3, 2),
        )
        self.assertEqual(
            out.status, "SHARED_PROTECTED_SINGLE_ENTRY_OBSERVED_NONAUTHORIZING"
        )
        before_stops[0]["takeProfitPrice"] = D("112")
        after = assess_shared_protected_entry(
            pre_witness=witness,
            pre_positions=self.pre_positions, pre_stops=before_stops,
            stage=self.stage, intent=self.intent,
            post_positions=self.post_positions,
            post_stops=[manual_tp, *self.post_stops],
            post_order=self.order, post_inventory=counts(3, 2),
        )
        self.assertNotEqual(
            after.status,
            "SHARED_PROTECTED_SINGLE_ENTRY_OBSERVED_NONAUTHORIZING",
        )

    async def test_four_existing_slots_and_conflicting_symbol_fail_closed(self):
        w = copy.deepcopy(self.pre_witness)
        w = type(w)(**{
            **w.__dict__,
            "account_positions_observed": 4,
            "manual_positions_observed": 4,
        })
        out = assess_shared_protected_entry(
            pre_witness=w,
            pre_positions=self.pre_positions, pre_stops=[],
            stage=self.stage, intent=self.intent,
            post_positions=self.post_positions, post_stops=self.post_stops,
            post_order=self.order, post_inventory=self.post_report,
        )
        self.assertNotEqual(
            out.status, "SHARED_PROTECTED_SINGLE_ENTRY_OBSERVED_NONAUTHORIZING",
        )
        # Manual already has this symbol, so staged new order can't own it.
        conflict = EntryIntent(
            self.intent.signal_id, "MANUALB_USDT",
            self.intent.reference_price, self.intent.notional_usdt,
            self.intent.volume_contracts, self.intent.stop_price,
            self.intent.external_oid,
        )
        result = assess_shared_protected_entry(
            pre_witness=self.pre_witness,
            pre_positions=self.pre_positions, pre_stops=[],
            stage=self.stage, intent=conflict,
            post_positions=self.post_positions,
            post_stops=self.post_stops,
            post_order=self.order, post_inventory=self.post_report,
        )
        self.assertEqual(
            result.status, "SHARED_PROTECTED_CANDIDATE_SYMBOL_CONFLICT",
        )

    def test_no_live_transport_or_database_write_in_module(self):
        import inspect
        import app.t100_live_shared_protected_entry_observer as m
        text = inspect.getsource(m)
        for forbidden in (
            "submit_short_with_stop(", "._private_post(",
            "asyncpg.", "MexcTradeClient(", ".send_one(",
        ):
            self.assertNotIn(forbidden, text)


if __name__ == "__main__":
    unittest.main()
