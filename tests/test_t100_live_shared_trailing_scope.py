"""Offline shared-account trailing-stop owner and manual safety regression.

One durable T100 CROSS short, one manual CROSS short, one manual ISOLATED
long. Only the T100 stop can tighten. No exchange POST or DB writes.
"""
from __future__ import annotations

import copy
import unittest
from dataclasses import replace
from decimal import Decimal as D

from app.t100_live_shared_trailing_scope import (
    observe_shared_t100_trailing_change,
)
from app.t100_live_stop_journal import StopAmendment
from app.t100_live_trailing import next_short_stop
from app.t100_live_four_slot_snapshot_coordinator import FourSlotSnapshotCoordinator
from tests.test_t100_live_shared_restart_recovery import SharedRestartTests
from tests.test_t100_live_shared_protected_entry_observer import counts


class SharedTrailingScopeTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        source = SharedRestartTests()
        await source.asyncSetUp()
        self.source = source
        self.witness = await source.restart()
        self.assertEqual(
            self.witness.status,
            "SHARED_ACCOUNT_MANAGED_PROVENANCE_MATCHED_NONAUTHORIZING",
        )
        self.checkpoint = source.prepared.entries[0].checkpoint
        self.before_positions = copy.deepcopy(source.after_positions)
        self.before_stops = copy.deepcopy(source.after_stops)
        self.after_positions = copy.deepcopy(self.before_positions)
        self.after_stops = copy.deepcopy(self.before_stops)
        step = next_short_stop(
            entry=self.checkpoint.entry_price,
            low=D("1.72"), current_stop=self.checkpoint.stop_price,
            best_profit_pct=self.checkpoint.best_profit_pct,
            price_tick=D("0.01"), armed=self.checkpoint.armed,
        )
        self.assertTrue(step.changed)
        self.assertEqual(step.stop_price, D("1.73"))
        self.amendment = StopAmendment.build(
            symbol=self.checkpoint.symbol,
            position_id=self.checkpoint.position_id,
            stop_order_id=self.checkpoint.stop_order_id,
            previous_stop=self.checkpoint.stop_price,
            proposed_stop=step.stop_price,
        )
        self.after_stops[0]["stopLossPrice"] = step.stop_price
        self.after_inventory = counts(3, 1)

    def observe(self, **changes):
        params = dict(
            pre_witness=self.witness,
            checkpoint=self.checkpoint,
            amendment=self.amendment,
            amendment_journal_state="SUBMITTING",
            before_positions=self.before_positions,
            before_stops=self.before_stops,
            after_positions=self.after_positions,
            after_stops=self.after_stops,
            after_inventory=self.after_inventory,
        )
        params.update(changes)
        return observe_shared_t100_trailing_change(**params)

    async def test_t100_trails_after_14pct_only_and_manual_positions_unchanged(self):
        report = self.observe()
        self.assertEqual(
            report.status,
            "SHARED_TRAILING_T100_OWNED_STOP_OBSERVED_NONAUTHORIZING",
        )
        self.assertTrue(report.owned_position_identity_matched)
        self.assertTrue(report.manual_and_other_owned_inventory_unchanged)
        self.assertTrue(report.native_stop_full_volume_observed)
        self.assertTrue(report.expected_tightening_observed)
        self.assertEqual(report.account_positions_observed, 3)
        self.assertEqual(report.manual_positions_observed, 2)
        self.assertFalse(report.independent_post_bookend_verified)
        self.assertFalse(report.amendment_durable_state_independently_verified)
        self.assertFalse(report.live_stop_mutation_allowed)
        self.assertFalse(report.live_authorization)
        self.assertFalse(report.mexc_orders_sent)

    async def test_change_to_manual_cross_or_isolated_position_blocks(self):
        for index in (0, 1):
            with self.subTest(index=index):
                changed = copy.deepcopy(self.after_positions)
                changed[index]["holdVol"] = D("21")
                self.assertEqual(
                    self.observe(after_positions=changed).status,
                    "SHARED_TRAILING_POSITION_DRIFT",
                )

    async def test_manual_tp_only_preserved_and_any_manual_tp_change_blocks(self):
        manual_tp = {
            "id": "901", "positionId": "11",
            "symbol": "MANUALB_USDT", "stopLossPrice": D("0"),
            "takeProfitPrice": D("6"),
        }
        self.source.venue.first_stops.append(copy.deepcopy(manual_tp))
        self.source.venue.last_stops.append(copy.deepcopy(manual_tp))
        self.source.complete.result = counts(3, 2)
        witness = await self.source.restart()
        self.assertEqual(
            witness.status,
            "SHARED_ACCOUNT_MANAGED_PROVENANCE_MATCHED_NONAUTHORIZING",
        )
        before = [copy.deepcopy(self.before_stops[0]), copy.deepcopy(manual_tp)]
        after = [copy.deepcopy(self.after_stops[0]), copy.deepcopy(manual_tp)]
        self.assertEqual(
            self.observe(
                pre_witness=witness, before_stops=before,
                after_stops=after, after_inventory=counts(3, 2),
            ).status,
            "SHARED_TRAILING_T100_OWNED_STOP_OBSERVED_NONAUTHORIZING",
        )
        after[1]["takeProfitPrice"] = D("7")
        self.assertEqual(
            self.observe(
                pre_witness=witness, before_stops=before,
                after_stops=after, after_inventory=counts(3, 2),
            ).status,
            "SHARED_TRAILING_MANUAL_OR_OTHER_STOP_DRIFT",
        )

    async def test_stop_missing_undercovered_loosened_or_finished_blocks(self):
        missing = self.observe(
            after_stops=[], after_inventory=counts(3, 0),
        )
        self.assertEqual(
            missing.status, "SHARED_TRAILING_TARGET_NATIVE_STOP_NOT_UNIQUE",
        )
        for diff in (
            {"vol": D("249")},
            {"stopLossPrice": D("1.74")},
            {"isFinished": 1},
            {"positionType": 1},
        ):
            with self.subTest(diff=diff):
                changed = [dict(self.after_stops[0], **diff)]
                self.assertEqual(
                    self.observe(after_stops=changed).status,
                    "SHARED_TRAILING_STOP_PROTECTION_INVALID",
                )

    async def test_replacement_requires_finished_old_stop_evidence(self):
        replacement = [dict(self.after_stops[0], id="931")]
        self.assertEqual(
            self.observe(after_stops=replacement).status,
            "SHARED_TRAILING_EXPECTED_NATIVE_CHANGE_UNVERIFIED",
        )
        finished = [{
            "id": "930", "positionId": "390",
            "symbol": "NEW_USDT", "state": 2,
            "triggerSide": 0, "isFinished": 1,
            "stopLossPrice": D("3.5"),
        }]
        result = self.observe(
            after_stops=replacement, finished_stops=finished,
        )
        self.assertEqual(
            result.status,
            "SHARED_TRAILING_T100_OWNED_STOP_OBSERVED_NONAUTHORIZING",
        )
        finished[0]["triggerSide"] = 2
        self.assertEqual(
            self.observe(
                after_stops=replacement, finished_stops=finished,
            ).status,
            "SHARED_TRAILING_EXPECTED_NATIVE_CHANGE_UNVERIFIED",
        )

    async def test_prepared_or_nonowned_journal_or_stale_checkpoint_blocks(self):
        self.assertEqual(
            self.observe(amendment_journal_state="PREPARED").status,
            "SHARED_TRAILING_WITNESS_OR_JOURNAL_STATE_UNVERIFIED",
        )
        wrong = replace(self.checkpoint, position_id="11")
        self.assertEqual(
            self.observe(checkpoint=wrong).status,
            "SHARED_TRAILING_T100_OWNERSHIP_OR_STOP_UNVERIFIED",
        )
        stale = replace(self.checkpoint, stop_price=D("3.49"))
        self.assertEqual(
            self.observe(checkpoint=stale).status,
            "SHARED_TRAILING_T100_OWNERSHIP_OR_STOP_UNVERIFIED",
        )
        unchanged = copy.deepcopy(self.before_stops)
        self.assertEqual(
            self.observe(after_stops=unchanged).status,
            "SHARED_TRAILING_STOP_PROTECTION_INVALID",
        )

    async def test_critical_before_snapshot_hash_required(self):
        changed = copy.deepcopy(self.before_positions)
        changed[0]["holdVol"] = D("999")
        self.assertEqual(
            self.observe(before_positions=changed).status,
            "SHARED_TRAILING_PRE_ACCOUNT_UNVERIFIED",
        )

    def test_never_contains_exchange_mutation_or_persist_calls(self):
        import inspect
        import app.t100_live_shared_trailing_scope as m
        source = inspect.getsource(m)
        for forbidden in (
            "submit_native_short_stop_tightening(",
            "._private_post(", "PostgresStopJournal(",
            ".send_one(", "LiveMutationPermit",
            "MexcTradeClient(",
        ):
            self.assertNotIn(forbidden, source)


if __name__ == "__main__":
    unittest.main()
