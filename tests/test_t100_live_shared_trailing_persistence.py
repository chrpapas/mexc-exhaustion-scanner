"""Offline shared account T100 trailing journal/checkpoint integration.

No MEXC mutations. Tests snapshot provenance and V40 policy BEFORE
existing durable journal advances. Manual CROSS short/ISOLATED long remain
stopless and unowned; checkpoint write failure recovers without second POST.
"""
from __future__ import annotations

import copy
import unittest
from dataclasses import replace
from decimal import Decimal as D

from app.t100_live_checkpoint_coordinator import CheckpointPersistenceCoordinator
from app.t100_live_shared_trailing_persistence import (
    SharedTrailingPersistencePipeline, SharedTrailingPipelineHalted,
)
from app.t100_live_stop_amendment_coordinator import StopAmendmentLifecycleCoordinator
from app.t100_live_stop_update_pipeline import StopUpdatePersistencePipeline
from tests.test_t100_live_shared_trailing_scope import SharedTrailingScopeTests
from tests.test_t100_live_stop_update_pipeline import Journal, Writer


class SharedTrailingPersistenceTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        src = SharedTrailingScopeTests()
        await src.asyncSetUp()
        self.src = src
        self.journal = Journal()
        self.writer = Writer()
        self.lifecycle = StopAmendmentLifecycleCoordinator(self.journal)
        self.inner = StopUpdatePersistencePipeline(
            self.lifecycle, CheckpointPersistenceCoordinator(self.writer),
        )
        self.pipeline = SharedTrailingPersistencePipeline(self.inner)
        self.amendment = src.amendment
        await self.lifecycle.prepare(self.proposal())
        await self.lifecycle.mark_submitting(self.amendment)

    def proposal(self):
        from app.t100_live_supervisor import StopProposal
        s = self.src
        return StopProposal(
            symbol=s.checkpoint.symbol,
            position_id=s.checkpoint.position_id,
            stop_order_id=s.checkpoint.stop_order_id,
            old_stop=s.checkpoint.stop_price,
            proposed_stop=self.amendment.proposed_stop,
            best_profit_pct=D("14"),
            armed=True,
        )

    def kwargs(self):
        s = self.src
        return dict(
            pre_witness=s.witness,
            checkpoint=s.checkpoint,
            amendment=self.amendment,
            before_positions=copy.deepcopy(s.before_positions),
            before_stops=copy.deepcopy(s.before_stops),
            after_positions_first=copy.deepcopy(s.after_positions),
            after_stops_first=copy.deepcopy(s.after_stops),
            after_inventory_first=copy.deepcopy(s.after_inventory),
            after_positions_second=copy.deepcopy(s.after_positions),
            after_stops_second=copy.deepcopy(s.after_stops),
            after_inventory_second=copy.deepcopy(s.after_inventory),
            observed_low=D("1.72"), price_tick=D("0.01"),
        )

    async def run_it(self, **updates):
        kwargs = self.kwargs()
        kwargs.update(updates)
        return await self.pipeline.reconcile_bookended_and_persist(**kwargs)

    async def test_t100_only_verified_and_written_without_manual_changes(self):
        original_manual = copy.deepcopy(self.src.before_positions[:2])
        out = await self.run_it()
        self.assertEqual(
            out.status, "SHARED_T100_TRAILING_CHECKPOINT_WRITTEN_NONAUTHORIZING"
        )
        self.assertEqual(out.checkpoint.stop_price, D("1.73"))
        self.assertEqual(out.checkpoint.best_profit_pct, D("14"))
        self.assertTrue(out.checkpoint.armed)
        self.assertEqual(out.checkpoint.checkpoint_version, 2)
        self.assertEqual(len(self.writer.calls), 1)
        self.assertEqual(self.journal.states[self.amendment.external_id], "VERIFIED")
        self.assertEqual(self.src.before_positions[:2], original_manual)
        self.assertFalse(out.exchange_mutation_sent_by_coordinator)
        self.assertFalse(out.operator_positions_mutated_by_coordinator)
        self.assertFalse(out.post_bookend_get_provenance_independently_certified)
        self.assertFalse(out.live_authorization)

    async def test_bookend_stop_price_or_manual_position_drift_rejects_without_writes(self):
        for k, v in (
            ("after_stops_second", [
                {**self.src.after_stops[0], "stopLossPrice": D("1.72")},
            ]),
            ("after_positions_second", [
                {**self.src.after_positions[0], "holdVol": D("777")},
                *self.src.after_positions[1:],
            ]),
        ):
            with self.subTest(k=k):
                with self.assertRaisesRegex(
                    SharedTrailingPipelineHalted, "POST_BOOKENDS_UNVERIFIED"
                ):
                    await self.run_it(**{k: v})
                self.assertEqual(self.writer.calls, [])
                self.assertEqual(
                    self.journal.states[self.amendment.external_id],
                    "SUBMITTING",
                )

    async def test_manual_position_unchanged_across_bookends_but_changed_from_pre(self):
        changed = copy.deepcopy(self.src.after_positions)
        changed[0]["holdVol"] = D("99")
        with self.assertRaisesRegex(
            SharedTrailingPipelineHalted, "ACCOUNT_OR_NATIVE_STOP_UNVERIFIED"
        ):
            await self.run_it(
                after_positions_first=changed,
                after_positions_second=copy.deepcopy(changed),
            )
        self.assertEqual(self.writer.calls, [])
        self.assertEqual(self.journal.states[self.amendment.external_id], "SUBMITTING")

    async def test_stop_missing_undercovered_or_manual_tp_modified(self):
        bad = copy.deepcopy(self.src.after_stops)
        bad[0]["vol"] = D("249")
        with self.assertRaises(SharedTrailingPipelineHalted):
            await self.run_it(
                after_stops_first=bad, after_stops_second=copy.deepcopy(bad),
            )
        bad = []
        from tests.test_t100_live_shared_protected_entry_observer import counts
        with self.assertRaises(SharedTrailingPipelineHalted):
            await self.run_it(
                after_stops_first=bad, after_stops_second=bad,
                after_inventory_first=counts(3, 0),
                after_inventory_second=counts(3, 0),
            )
        self.assertEqual(self.writer.calls, [])

    async def test_frozen_v40_stop_and_trailing_best_are_not_mutable(self):
        with self.assertRaisesRegex(
            SharedTrailingPipelineHalted, "FROZEN_POLICY_OR_IDENTITY_MISMATCH"
        ):
            await self.run_it(observed_low=D("1.90"))
        wrong = replace(self.amendment, proposed_stop=D("1.70"))
        with self.assertRaisesRegex(
            SharedTrailingPipelineHalted, "FROZEN_POLICY_OR_IDENTITY_MISMATCH"
        ):
            await self.run_it(amendment=wrong)
        self.assertEqual(self.writer.calls, [])

    async def test_prepared_and_unknown_journal_reconcile_strictly(self):
        self.journal.states[self.amendment.external_id] = "PREPARED"
        with self.assertRaisesRegex(
            SharedTrailingPipelineHalted, "AMENDMENT_NOT_SUBMITTED"
        ):
            await self.run_it()
        self.assertEqual(self.writer.calls, [])
        self.journal.states[self.amendment.external_id] = "UNKNOWN"
        out = await self.run_it(restart_reconciliation=True)
        self.assertEqual(out.checkpoint.checkpoint_version, 2)
        self.assertEqual(self.journal.states[self.amendment.external_id], "VERIFIED")

    async def test_failed_checkpoint_write_recovers_without_transport_retry(self):
        self.writer.fail = True
        with self.assertRaisesRegex(
            SharedTrailingPipelineHalted, "CHECKPOINT_PERSISTENCE_FAILED"
        ):
            await self.run_it()
        self.assertEqual(self.writer.calls, [])
        self.assertEqual(self.journal.states[self.amendment.external_id], "VERIFIED")
        self.writer.fail = False
        recovered = await self.run_it(restart_reconciliation=True)
        self.assertEqual(recovered.checkpoint.stop_price, D("1.73"))
        self.assertEqual(recovered.checkpoint.checkpoint_version, 2)
        self.assertEqual(len(self.writer.calls), 1)
        self.assertFalse(recovered.exchange_mutation_sent_by_coordinator)

    def test_no_orders_or_venue_transport_calls(self):
        import inspect
        import app.t100_live_shared_trailing_persistence as mod
        src = inspect.getsource(mod)
        for forbidden in (
            "submit_native_short_stop_tightening(", "MexcTradeClient(",
            "._private_post(", ".send_one(", "LiveMutationPermit",
        ):
            self.assertNotIn(forbidden, src)


if __name__ == "__main__":
    unittest.main()
