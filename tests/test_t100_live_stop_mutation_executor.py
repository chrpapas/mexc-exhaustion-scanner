"""Offline exactly-once-attempt stop mutation tests."""
import unittest
from dataclasses import replace
from decimal import Decimal as D

from app.t100_live_authorization import (
    LivePilotReadinessEvidence,
    issue_live_mutation_permit,
)
from app.t100_live_guards import LIVE_CONFIRMATION, T100ExecutionControls
from app.t100_live_stop_amendment_coordinator import (
    StopAmendmentLifecycleCoordinator,
)
from app.t100_live_stop_mutation_executor import (
    StopMutationExecutionHalted,
    StopMutationExecutor,
)
from app.t100_live_supervisor import StopProposal


def permit():
    controls = T100ExecutionControls.from_env({
        "T100_EXECUTION_MODE": "live",
        "T100_LIVE_ORDER_API_ENABLED": "true",
        "T100_LIVE_KILL_SWITCH": "false",
        "T100_LIVE_OPERATOR_CONFIRM": LIVE_CONFIRMATION,
        "T100_LIVE_MAX_POSITION_NOTIONAL_USDT": "100",
        "T100_LIVE_MAX_TOTAL_NOTIONAL_USDT": "100",
        "T100_LIVE_MAX_DAILY_LOSS_USDT": "25",
        "T100_LIVE_LEVERAGE": "1",
    })
    evidence = LivePilotReadinessEvidence(
        code_revision_verified=True,
        strategy_parity_verified=True,
        durable_schema_deployed_verified=True,
        current_inventory_completeness_verified=True,
        account_reconciled=True,
        protected_position_soak_verified=True,
        restart_recovery_verified=True,
        native_stop_mutation_contract_verified=True,
        entry_reconciliation_verified=True,
        fill_and_accounting_verified=True,
        private_ws_pre_pilot_contract_verified=True,
        unresolved_mutations_absent=True,
        independent_review_completed=True,
        operator_tiny_pilot_authorized=True,
    )
    return issue_live_mutation_permit(controls, evidence)


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


class Transport:
    def __init__(self):
        self.posts = 0
        self.fail = False
        self.args = None

    async def submit_native_short_stop_tightening(self, **kwargs):
        self.posts += 1
        self.args = kwargs
        if self.fail:
            raise TimeoutError("ambiguous")


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


class StopMutationExecutorTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.journal = Journal()
        self.lifecycle = StopAmendmentLifecycleCoordinator(self.journal)
        self.transport = Transport()
        self.executor = StopMutationExecutor(
            lifecycle=self.lifecycle,
            transport=self.transport,
        )
        self.amendment, _ = await self.lifecycle.prepare(proposal())

    async def test_prepared_moves_to_submitting_before_one_post(self):
        result = await self.executor.submit_once(
            self.amendment,
            permit=permit(),
        )
        self.assertEqual(self.transport.posts, 1)
        self.assertEqual(
            self.journal.states[self.amendment.external_id],
            "SUBMITTING",
        )
        self.assertEqual(result.journal_state, "SUBMITTING")
        self.assertTrue(result.reconciliation_required)
        self.assertFalse(result.live_authorization)
        self.assertEqual(
            self.transport.args["stop_order_id"], 99
        )
        self.assertEqual(
            self.transport.args["previous_stop_price"], "175"
        )
        self.assertEqual(
            self.transport.args["new_stop_price"], "90"
        )

    async def test_ambiguous_post_becomes_unknown_and_never_retries(self):
        self.transport.fail = True
        with self.assertRaisesRegex(
            StopMutationExecutionHalted,
            "OUTCOME_UNKNOWN_RECONCILE_ONLY",
        ):
            await self.executor.submit_once(
                self.amendment,
                permit=permit(),
            )
        self.assertEqual(self.transport.posts, 1)
        self.assertEqual(
            self.journal.states[self.amendment.external_id],
            "UNKNOWN",
        )

        self.transport.fail = False
        with self.assertRaisesRegex(
            StopMutationExecutionHalted,
            "RECONCILE_ONLY:UNKNOWN",
        ):
            await self.executor.submit_once(
                self.amendment,
                permit=permit(),
            )
        self.assertEqual(self.transport.posts, 1)

    async def test_submitting_state_never_emits_second_post(self):
        await self.lifecycle.mark_submitting(self.amendment)
        with self.assertRaisesRegex(
            StopMutationExecutionHalted,
            "RECONCILE_ONLY:SUBMITTING",
        ):
            await self.executor.submit_once(
                self.amendment,
                permit=permit(),
            )
        self.assertEqual(self.transport.posts, 0)

    async def test_verified_state_never_emits_post(self):
        await self.lifecycle.mark_submitting(self.amendment)
        self.journal.states[self.amendment.external_id] = "VERIFIED"
        self.journal.verified[self.amendment.external_id] = "99"
        with self.assertRaisesRegex(
            StopMutationExecutionHalted,
            "RECONCILE_ONLY:VERIFIED",
        ):
            await self.executor.submit_once(
                self.amendment,
                permit=permit(),
            )
        self.assertEqual(self.transport.posts, 0)

    async def test_invalid_stop_id_blocks_before_submitting(self):
        bad = replace(self.amendment, stop_order_id="not-numeric")
        with self.assertRaisesRegex(
            StopMutationExecutionHalted,
            "STOP_ORDER_ID_INVALID_BEFORE_SUBMISSION",
        ):
            await self.executor.submit_once(
                bad,
                permit=permit(),
            )
        self.assertEqual(
            self.journal.states[self.amendment.external_id],
            "PREPARED",
        )
        self.assertEqual(self.transport.posts, 0)

    async def test_missing_permit_blocks_before_journal_or_post(self):
        with self.assertRaises(Exception):
            await self.executor.submit_once(
                self.amendment,
                permit=None,
            )
        self.assertEqual(
            self.journal.states[self.amendment.external_id],
            "PREPARED",
        )
        self.assertEqual(self.transport.posts, 0)


if __name__ == "__main__":
    unittest.main()
