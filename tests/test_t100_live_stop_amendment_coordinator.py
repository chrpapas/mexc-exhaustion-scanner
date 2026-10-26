"""Offline tests for crash-safe stop amendment lifecycle coordination."""
import unittest
from decimal import Decimal as D

from app.t100_live_stop_amendment_coordinator import (
    StopAmendmentCoordinatorHalted,
    StopAmendmentLifecycleCoordinator,
)
from app.t100_live_supervisor import StopProposal


class Journal:
    def __init__(self):
        self.states = {}
        self.verified = {}
        self.transitions = []

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
        self.transitions.append((expected, target))
        if self.states[external_id] != expected:
            return False
        self.states[external_id] = target
        return True

    async def mark_verified(
        self, external_id, expected, verified_stop_order_id
    ):
        self.transitions.append((expected, "VERIFIED"))
        if self.states[external_id] != expected:
            return False
        self.states[external_id] = "VERIFIED"
        self.verified[external_id] = verified_stop_order_id
        return True


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


def current_stop(**extra):
    row = {
        "id": "99",
        "positionId": "10",
        "symbol": "SIREN_USDT",
        "state": 1,
        "isFinished": 0,
        "stopLossPrice": "175",
    }
    row.update(extra)
    return row


def finished_old(**extra):
    row = {
        "id": "99",
        "positionId": "10",
        "symbol": "SIREN_USDT",
        "state": 2,
        "triggerSide": 0,
        "isFinished": 1,
        "stopLossPrice": "175",
    }
    row.update(extra)
    return row


class StopAmendmentCoordinatorTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.journal = Journal()
        self.coordinator = StopAmendmentLifecycleCoordinator(
            self.journal
        )
        self.amendment, state = await self.coordinator.prepare(
            proposal()
        )
        self.assertEqual(state, "PREPARED")

    async def test_prepare_is_deterministic_and_idempotent(self):
        amendment2, state = await self.coordinator.prepare(proposal())
        self.assertEqual(amendment2.external_id, self.amendment.external_id)
        self.assertEqual(state, "EXISTING_PREPARED")

    async def test_prepared_change_is_not_claimed_as_trader_owned(self):
        with self.assertRaisesRegex(
            StopAmendmentCoordinatorHalted, "NOT_OWNED"
        ):
            await self.coordinator.reconcile_observed_transition(
                self.amendment,
                before_stops=[current_stop()],
                after_stops=[current_stop(stopLossPrice="90")],
            )

    async def test_in_place_transition_verifies_after_submitting(self):
        await self.coordinator.mark_submitting(self.amendment)
        outcome = await self.coordinator.reconcile_observed_transition(
            self.amendment,
            before_stops=[current_stop()],
            after_stops=[current_stop(stopLossPrice="90")],
        )
        self.assertEqual(outcome.verified_stop_order_id, "99")
        self.assertFalse(outcome.replacement_stop_identity)
        self.assertFalse(outcome.live_authorization)
        self.assertEqual(
            self.journal.states[self.amendment.external_id],
            "VERIFIED",
        )

    async def test_replacement_requires_finished_cancel_provenance(self):
        await self.coordinator.mark_submitting(self.amendment)
        with self.assertRaisesRegex(
            StopAmendmentCoordinatorHalted,
            "TERMINAL_PROVENANCE_REQUIRED",
        ):
            await self.coordinator.reconcile_observed_transition(
                self.amendment,
                before_stops=[current_stop()],
                after_stops=[
                    current_stop(id="100", stopLossPrice="90")
                ],
            )
        self.assertEqual(
            self.journal.states[self.amendment.external_id],
            "SUBMITTING",
        )

    async def test_replacement_verifies_and_persists_resulting_id(self):
        await self.coordinator.mark_submitting(self.amendment)
        outcome = await self.coordinator.reconcile_observed_transition(
            self.amendment,
            before_stops=[current_stop()],
            after_stops=[current_stop(id="100", stopLossPrice="89")],
            finished_stops=[finished_old()],
        )
        self.assertTrue(outcome.replacement_stop_identity)
        self.assertEqual(outcome.verified_stop_order_id, "100")
        self.assertEqual(
            self.journal.verified[self.amendment.external_id],
            "100",
        )

    async def test_unknown_outcome_can_reconcile_without_retry(self):
        await self.coordinator.mark_submitting(self.amendment)
        await self.coordinator.mark_unknown(self.amendment)
        outcome = await self.coordinator.reconcile_observed_transition(
            self.amendment,
            before_stops=[current_stop()],
            after_stops=[current_stop(stopLossPrice="90")],
        )
        self.assertEqual(outcome.verified_stop_order_id, "99")
        self.assertEqual(
            self.journal.states[self.amendment.external_id],
            "VERIFIED",
        )

    async def test_protection_gap_never_verifies(self):
        await self.coordinator.mark_submitting(self.amendment)
        with self.assertRaisesRegex(
            StopAmendmentCoordinatorHalted,
            "PROTECTION_GAP_NOT_ACCEPTED",
        ):
            await self.coordinator.reconcile_observed_transition(
                self.amendment,
                before_stops=[current_stop()],
                after_stops=[],
            )
        self.assertEqual(
            self.journal.states[self.amendment.external_id],
            "SUBMITTING",
        )

    async def test_verified_recheck_requires_same_persisted_result_id(self):
        await self.coordinator.mark_submitting(self.amendment)
        await self.coordinator.reconcile_observed_transition(
            self.amendment,
            before_stops=[current_stop()],
            after_stops=[current_stop(stopLossPrice="90")],
        )
        self.journal.verified[self.amendment.external_id] = "100"
        with self.assertRaisesRegex(
            StopAmendmentCoordinatorHalted,
            "PERSISTENCE_MISMATCH",
        ):
            await self.coordinator.reconcile_observed_transition(
                self.amendment,
                before_stops=[current_stop()],
                after_stops=[current_stop(stopLossPrice="90")],
            )

    async def test_restart_current_outcome_verifies_unknown_without_retry(self):
        await self.coordinator.mark_submitting(self.amendment)
        await self.coordinator.mark_unknown(self.amendment)
        outcome = await self.coordinator.reconcile_current_outcome(
            self.amendment,
            current_stops=[current_stop(stopLossPrice="90")],
        )
        self.assertEqual(outcome.verified_stop_order_id, "99")
        self.assertEqual(
            self.journal.states[self.amendment.external_id],
            "VERIFIED",
        )

    async def test_restart_unchanged_stop_remains_unresolved(self):
        await self.coordinator.mark_submitting(self.amendment)
        await self.coordinator.mark_unknown(self.amendment)
        with self.assertRaisesRegex(
            StopAmendmentCoordinatorHalted,
            "CURRENT_STOP_OUTCOME_UNRESOLVED",
        ):
            await self.coordinator.reconcile_current_outcome(
                self.amendment,
                current_stops=[current_stop(stopLossPrice="175")],
            )
        self.assertEqual(
            self.journal.states[self.amendment.external_id],
            "UNKNOWN",
        )

    async def test_restart_replacement_verifies_with_terminal_history(self):
        await self.coordinator.mark_submitting(self.amendment)
        await self.coordinator.mark_unknown(self.amendment)
        outcome = await self.coordinator.reconcile_current_outcome(
            self.amendment,
            current_stops=[
                current_stop(id="100", stopLossPrice="89")
            ],
            finished_stops=[finished_old()],
        )
        self.assertEqual(outcome.verified_stop_order_id, "100")
        self.assertTrue(outcome.replacement_stop_identity)

    async def test_unknown_requires_submitting(self):
        with self.assertRaisesRegex(
            StopAmendmentCoordinatorHalted,
            "UNKNOWN_REQUIRES_SUBMITTING",
        ):
            await self.coordinator.mark_unknown(self.amendment)


if __name__ == "__main__":
    unittest.main()
