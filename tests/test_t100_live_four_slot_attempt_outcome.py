"""Full four-slot journal-to-GET-only protected-fill observation regressions.

The simulated order/FILL is produced only in the test fixture, NOT by the
production gateway. These tests must never use MEXC POST or enable live mode.
"""
from __future__ import annotations

import copy
from dataclasses import replace
from decimal import Decimal as D
import unittest

from app.t100_live_execution import EntryIntent
from app.t100_live_four_slot_attempt_journal import DurableAttemptStage
from app.t100_live_four_slot_attempt_outcome import (
    assess_staged_protected_attempts, observe_staged_protected_attempts,
)
from app.t100_live_four_slot_snapshot_coordinator import (
    FourSlotSnapshotCoordinator,
)
from tests.test_t100_live_four_slot_snapshot_coordinator import (
    fixture, Ledger, Venue, Completeness, report,
)
import tests.test_t100_live_four_slot_attempt_journal as stage_tests


class ProtectedOutcomeTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        # Uses the REAL existing 3-slot protected ownership observer, then
        # REAL reservation + SUBMITTING journal against an isolated fake DB.
        previous = stage_tests.AttemptJournalTests()
        await previous.reset(3)
        reservation = await previous.reserve(410, "FOURTH_USDT")
        stage = await previous.journal.stage(**previous.params(reservation))
        self.assertEqual(previous.pg.new_intents[0]["state"], "SUBMITTING")
        self.assertEqual(previous.pg.reserved[0]["state"], "SUBMITTING")
        self.assertFalse(stage.post_attempted)
        self.before = previous.witness
        self.stage = stage
        self.intent = previous.intent(reservation)
        self.pg = previous.pg

        snap, oldorders, positions, stops = fixture(3)
        for p in positions:
            p["openType"] = 2
        for s in stops:
            s.update({"isFinished": 0, "positionType": 2})
        new_order = {
            "externalOid": reservation.external_oid, "orderId": "990",
            "symbol": "FOURTH_USDT", "side": 3, "state": 3,
            "positionId": "900", "dealVol": D("250"),
            "dealAvgPrice": D("2"),
        }
        positions.append({
            "symbol": "FOURTH_USDT", "positionId": "900",
            "positionType": 2, "openType": 2, "state": 1,
            "holdVol": D("250"), "holdAvgPrice": D("2"),
        })
        stops.append({
            "symbol": "FOURTH_USDT", "positionId": "900",
            "id": "999", "state": 1, "isFinished": 0,
            "positionType": 2, "stopLossPrice": D("3.5"),
            "vol": D("250"),
        })
        self.venue = Venue([*oldorders, new_order], positions, stops)
        self.complete = Completeness(report(4))

    async def observe(self):
        return await observe_staged_protected_attempts(
            stages=[self.stage], intents=[self.intent],
            pre_attempt_witness=self.before, venue=self.venue,
            completeness=self.complete,
        )

    async def test_three_managed_plus_fourth_fill_native_stop_bookended(self):
        r = await self.observe()
        self.assertEqual(
            r.status, "ATTEMPT_ALL_NEW_POSITIONS_PROTECTED_OBSERVED_NONAUTHORIZING",
        )
        self.assertEqual(r.observed_positions, 4)
        self.assertEqual(r.preexisting_owned_positions, 3)
        self.assertEqual(r.newly_observed_protected_positions, 1)
        self.assertTrue(r.native_stop_witness_observed)
        self.assertTrue(r.all_staged_attempts_fully_filled_and_protected)
        self.assertFalse(r.durable_ack_persisted)
        self.assertFalse(r.durable_checkpoint_persisted)
        self.assertFalse(r.exchange_atomicity_verified)
        self.assertFalse(r.entry_allowed)
        self.assertFalse(r.live_authorization)
        self.assertEqual(self.venue.positions_reads, 2)
        self.assertEqual(self.venue.stop_reads, 2)
        self.assertEqual(self.venue.order_reads, 2)
        self.assertEqual(self.pg.new_intents[0]["state"], "SUBMITTING")
        self.assertEqual(self.pg.reserved[0]["state"], "SUBMITTING")

    async def test_partial_fill_is_never_marked_protected(self):
        key = ("FOURTH_USDT", self.intent.external_oid)
        self.venue.orders[key]["dealVol"] = D("200")
        result = await self.observe()
        self.assertEqual(result.status, "ATTEMPT_PARTIAL_OR_MISMATCHED_FILL_BLOCK")
        self.assertFalse(result.live_authorization)

    async def test_missing_or_insufficient_native_stop_is_blocked(self):
        self.venue.first_stops[-1]["vol"] = D("100")
        self.venue.last_stops[-1]["vol"] = D("100")
        self.assertEqual((await self.observe()).status,
                         "ATTEMPT_NATIVE_STOP_OR_POSITION_UNVERIFIED")
        self.venue.first_stops.pop()
        self.venue.last_stops.pop()
        self.assertNotEqual(
            (await self.observe()).status,
            "ATTEMPT_ALL_NEW_POSITIONS_PROTECTED_OBSERVED_NONAUTHORIZING",
        )

    async def test_invalid_stop_trigger_or_closed_stop_never_reconciles(self):
        for attr, value in (
            ("stopLossPrice", D("3.6")),
            ("isFinished", 1),
            ("positionType", 1),
            ("state", 2),
        ):
            with self.subTest(attr=attr):
                for rows in (self.venue.first_stops, self.venue.last_stops):
                    rows[-1][attr] = value
                self.assertEqual(
                    (await self.observe()).status,
                    "ATTEMPT_NATIVE_STOP_OR_POSITION_UNVERIFIED",
                )
                self.venue.stop_reads = 0
                for rows in (self.venue.first_stops, self.venue.last_stops):
                    rows[-1][attr] = {
                        "stopLossPrice": D("3.5"), "isFinished": 0,
                        "positionType": 2, "state": 1,
                    }[attr]

    async def test_exact_get_missing_must_not_trigger_retry(self):
        self.venue.orders.clear()
        r = await self.observe()
        self.assertEqual(r.status, "ATTEMPT_EXACT_ORDER_GET_UNRESOLVED_NO_RETRY")
        self.assertFalse(r.mexc_orders_sent)
        self.assertEqual(self.pg.new_intents[0]["state"], "SUBMITTING")

    async def test_preexisting_stop_identity_drift_blocks(self):
        self.venue.first_stops[0]["id"] = "CHANGED"
        self.venue.last_stops[0]["id"] = "CHANGED"
        self.assertEqual((await self.observe()).status,
                         "ATTEMPT_PREEXISTING_POSITION_CHANGED")

    async def test_foreign_manual_position_blocks_even_with_normal_counts(self):
        for rows in (self.venue.first_positions, self.venue.last_positions):
            rows[0]["symbol"] = "UNOWNED_USDT"
        self.assertEqual((await self.observe()).status,
                         "ATTEMPT_PREEXISTING_POSITION_NOT_PRESERVED")

    async def test_cross_margin_mismatch_fails_closed(self):
        for rows in (self.venue.first_positions, self.venue.last_positions):
            rows[-1]["openType"] = 1
        self.assertEqual((await self.observe()).status,
                         "ATTEMPT_NATIVE_STOP_OR_POSITION_UNVERIFIED")

    async def test_duplicate_stop_order_identity_blocks(self):
        for rows in (self.venue.first_stops, self.venue.last_stops):
            rows[-1]["id"] = rows[0]["id"]
        self.assertEqual((await self.observe()).status,
                         "ATTEMPT_EXCHANGE_ROWS_MALFORMED")

    async def test_incomplete_account_or_unrelated_limit_order_blocks(self):
        self.complete.result["account_wide_completeness_verified"] = False
        self.assertEqual((await self.observe()).status,
                         "ATTEMPT_ACCOUNT_COMPLETENESS_OR_OPEN_ORDERS_BLOCK")
        self.complete.result["account_wide_completeness_verified"] = True
        self.complete.result["open_order_counts"]["limitOrderCount"] = 1
        self.complete.result["open_order_counts"]["sumCount"] = 5
        self.assertEqual((await self.observe()).status,
                         "ATTEMPT_ACCOUNT_COMPLETENESS_OR_OPEN_ORDERS_BLOCK")

    async def test_critical_position_drift_blocks_before_reconcile(self):
        self.venue.last_positions[-1]["holdVol"] = D("251")
        result = await self.observe()
        self.assertEqual(result.status,
                         "ATTEMPT_BOOKEND_CRITICAL_DRIFT_OR_ORDER_CHANGE")
        self.assertFalse(result.live_authorization)

    async def test_bookend_change_in_exact_order_blocks(self):
        original = self.venue.order_by_external_oid
        n = 0
        async def shifting(symbol, oid):
            nonlocal n
            n += 1
            found = await original(symbol, oid)
            if found and n == 2:
                found["dealVol"] = D("249")
            return found
        self.venue.order_by_external_oid = shifting
        self.assertEqual((await self.observe()).status,
                         "ATTEMPT_BOOKEND_CRITICAL_DRIFT_OR_ORDER_CHANGE")

    async def test_entire_position_native_stop_semantics_observed(self):
        for rows in (self.venue.first_stops, self.venue.last_stops):
            rows[-1].update({
                "volType": 2, "profitLossVolType": "SAME", "vol": 0,
            })
        r = await self.observe()
        self.assertEqual(
            r.status, "ATTEMPT_ALL_NEW_POSITIONS_PROTECTED_OBSERVED_NONAUTHORIZING",
        )

    async def test_staged_identity_mismatch_blocks_before_venue_get(self):
        bad = replace(self.stage, external_oid="different")
        r = await observe_staged_protected_attempts(
            stages=[bad], intents=[self.intent],
            pre_attempt_witness=self.before, venue=self.venue,
            completeness=self.complete,
        )
        self.assertEqual(r.status, "ATTEMPT_STAGE_IDENTITY_OR_OWNERSHIP_INVALID")
        self.assertFalse(r.entry_allowed)

    def test_no_order_transport_import_or_venue_mutations(self):
        import inspect
        import app.t100_live_four_slot_attempt_outcome as module
        src = inspect.getsource(module)
        for forbidden in (
            "MexcTradeClient(", "submit_short_with_stop(", "LiveMutationPermit",
            "._private_post(", "httpx.", "asyncpg.", ".send_one(",
        ):
            self.assertNotIn(forbidden, src)


if __name__ == "__main__":
    unittest.main()
