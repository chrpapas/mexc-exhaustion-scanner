"""Offline end-to-end tests for no-POST stop update persistence."""
import unittest
from decimal import Decimal as D

from app.t100_live_checkpoint_coordinator import (
    CheckpointPersistenceCoordinator,
)
from app.t100_live_checkpoint_journal import CheckpointJournalHalted
from app.t100_live_durable_recovery import PositionCheckpoint
from app.t100_live_stop_amendment_coordinator import (
    StopAmendmentLifecycleCoordinator,
)
from app.t100_live_stop_update_pipeline import (
    StopUpdatePersistencePipeline,
    StopUpdatePipelineHalted,
)
from app.t100_live_supervisor import StopProposal


class Journal:
    def __init__(self):
        self.states = {}
        self.verified = {}

    async def prepare(self, amendment):
        if amendment.external_id in self.states:
            return False
        self.states[amendment.external_id] = "PREPARED"
        return True

    async def state(self, external_id):
        return self.states[external_id]

    async def verified_stop_order_id(self, external_id):
        return self.verified.get(external_id)

    async def transition(self, external_id, expected, target):
        if self.states[external_id] != expected:
            return False
        self.states[external_id] = target
        return True

    async def mark_verified(
        self, external_id, expected, verified_stop_order_id
    ):
        if self.states[external_id] != expected:
            return False
        self.states[external_id] = "VERIFIED"
        self.verified[external_id] = verified_stop_order_id
        return True


class Writer:
    def __init__(self):
        self.calls = []
        self.fail = False

    async def create_initial(self, current):
        raise AssertionError("not used")

    async def advance(self, previous, current, *, reason):
        if self.fail:
            raise CheckpointJournalHalted("simulated db failure")
        self.calls.append((previous, current, reason))


def cp():
    return PositionCheckpoint(
        symbol="SIREN_USDT",
        external_oid="entry-1",
        exchange_order_id="order-1",
        position_id="10",
        contracts=D("20"),
        entry_price=D("100"),
        stop_order_id="99",
        stop_price=D("175"),
        best_profit_pct=D("0"),
        armed=False,
        lifecycle_state="OPEN",
        checkpoint_version=1,
    )


def proposal():
    return StopProposal(
        symbol="SIREN_USDT",
        position_id="10",
        stop_order_id="99",
        old_stop=D("175"),
        proposed_stop=D("90"),
        best_profit_pct=D("14"),
        armed=True,
    )


def stop(**kw):
    row = {
        "id": "99",
        "positionId": "10",
        "symbol": "SIREN_USDT",
        "state": 1,
        "isFinished": 0,
        "stopLossPrice": D("175"),
        "vol": D("20"),
    }
    row.update(kw)
    return row


def finished_old():
    return {
        "id": "99",
        "positionId": "10",
        "symbol": "SIREN_USDT",
        "state": 2,
        "triggerSide": 0,
        "isFinished": 1,
        "stopLossPrice": D("175"),
    }


def gates(**kw):
    data = {
        "evidence_fresh": True,
        "account_reconciled": True,
        "positions_complete": True,
        "stops_complete": True,
    }
    data.update(kw)
    return data


class StopUpdatePipelineTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.journal = Journal()
        self.writer = Writer()
        self.amendments = StopAmendmentLifecycleCoordinator(
            self.journal
        )
        self.checkpoints = CheckpointPersistenceCoordinator(
            self.writer
        )
        self.pipeline = StopUpdatePersistencePipeline(
            self.amendments,
            self.checkpoints,
        )
        self.amendment, _ = await self.amendments.prepare(proposal())
        await self.amendments.mark_submitting(self.amendment)

    async def test_in_place_amendment_persists_checkpoint(self):
        result = await self.pipeline.reconcile_observed_and_persist(
            cp(),
            self.amendment,
            before_stops=[stop()],
            after_stops=[stop(stopLossPrice=D("90"))],
            best_profit_pct=D("14"),
            armed=True,
            **gates(),
        )
        self.assertEqual(result.checkpoint.stop_order_id, "99")
        self.assertEqual(result.checkpoint.stop_price, D("90"))
        self.assertEqual(result.checkpoint.checkpoint_version, 2)
        self.assertFalse(result.live_authorization)
        self.assertEqual(
            self.journal.states[self.amendment.external_id],
            "VERIFIED",
        )

    async def test_replacement_amendment_persists_new_stop_identity(self):
        result = await self.pipeline.reconcile_observed_and_persist(
            cp(),
            self.amendment,
            before_stops=[stop()],
            after_stops=[
                stop(id="100", stopLossPrice=D("89"))
            ],
            finished_stops=[finished_old()],
            best_profit_pct=D("14.5"),
            armed=True,
            **gates(),
        )
        self.assertEqual(result.checkpoint.stop_order_id, "100")
        self.assertEqual(result.checkpoint.stop_price, D("89"))

    async def test_checkpoint_failure_leaves_verified_journal_for_recovery(self):
        self.writer.fail = True
        with self.assertRaisesRegex(
            StopUpdatePipelineHalted,
            "CHECKPOINT_PERSISTENCE_FAILED",
        ):
            await self.pipeline.reconcile_observed_and_persist(
                cp(),
                self.amendment,
                before_stops=[stop()],
                after_stops=[stop(stopLossPrice=D("90"))],
                best_profit_pct=D("14"),
                armed=True,
                **gates(),
            )
        self.assertEqual(
            self.journal.states[self.amendment.external_id],
            "VERIFIED",
        )
        self.assertEqual(
            self.journal.verified[self.amendment.external_id],
            "99",
        )

        self.writer.fail = False
        recovered = await self.pipeline.reconcile_current_and_persist(
            cp(),
            self.amendment,
            current_stops=[stop(stopLossPrice=D("90"))],
            best_profit_pct=D("14"),
            armed=True,
            **gates(),
        )
        self.assertEqual(recovered.checkpoint.stop_price, D("90"))
        self.assertEqual(recovered.checkpoint.checkpoint_version, 2)

    async def test_incomplete_inventory_blocks_checkpoint_after_verification(self):
        with self.assertRaisesRegex(
            StopUpdatePipelineHalted,
            "STOPS_INCOMPLETE",
        ):
            await self.pipeline.reconcile_observed_and_persist(
                cp(),
                self.amendment,
                before_stops=[stop()],
                after_stops=[stop(stopLossPrice=D("90"))],
                best_profit_pct=D("14"),
                armed=True,
                **gates(stops_complete=False),
            )
        self.assertEqual(
            self.journal.states[self.amendment.external_id],
            "VERIFIED",
        )


if __name__ == "__main__":
    unittest.main()
