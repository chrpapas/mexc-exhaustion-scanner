"""Offline typed live-mutation authorization policy tests."""
import unittest
from decimal import Decimal as D

from app.t100_live_authorization import (
    LiveAuthorizationHalted,
    LiveMutationPermit,
    LivePilotReadinessEvidence,
    issue_live_mutation_permit,
    require_live_mutation_permit,
)
from app.t100_live_guards import (
    LIVE_CONFIRMATION,
    T100ExecutionControls,
)


def controls(**overrides):
    env = {
        "T100_EXECUTION_MODE": "live",
        "T100_LIVE_ORDER_API_ENABLED": "true",
        "T100_LIVE_KILL_SWITCH": "false",
        "T100_LIVE_OPERATOR_CONFIRM": LIVE_CONFIRMATION,
        "T100_LIVE_MAX_POSITION_NOTIONAL_USDT": "100",
        "T100_LIVE_MAX_TOTAL_NOTIONAL_USDT": "100",
        "T100_LIVE_MAX_DAILY_LOSS_USDT": "25",
        "T100_LIVE_LEVERAGE": "1",
    }
    env.update(overrides)
    return T100ExecutionControls.from_env(env)


def evidence(**overrides):
    data = dict(
        code_revision_verified=True,
        durable_schema_deployed_verified=True,
        current_inventory_completeness_verified=True,
        account_reconciled=True,
        protected_position_soak_verified=True,
        restart_recovery_verified=True,
        native_stop_mutation_contract_verified=True,
        entry_reconciliation_verified=True,
        fill_and_accounting_verified=True,
        private_ws_lifecycle_semantics_verified=True,
        unresolved_mutations_absent=True,
        independent_review_completed=True,
        operator_tiny_pilot_authorized=True,
    )
    data.update(overrides)
    return LivePilotReadinessEvidence(**data)


class AuthorizationTests(unittest.TestCase):
    def test_all_gates_issue_typed_tiny_pilot_permit(self):
        p = issue_live_mutation_permit(controls(), evidence())
        self.assertEqual(p.scope, "T100_TINY_PILOT")
        self.assertEqual(p.max_position_notional_usdt, D("100"))
        self.assertEqual(p.leverage, 1)
        self.assertIs(require_live_mutation_permit(p), p)

    def test_paper_mode_can_never_issue_permit(self):
        with self.assertRaisesRegex(
            LiveAuthorizationHalted,
            "execution_mode_not_live",
        ):
            issue_live_mutation_permit(
                controls(T100_EXECUTION_MODE="paper"),
                evidence(),
            )

    def test_each_release_gate_is_mandatory(self):
        fields = tuple(evidence().__dataclass_fields__)
        for field in fields:
            with self.subTest(field=field):
                with self.assertRaises(LiveAuthorizationHalted):
                    issue_live_mutation_permit(
                        controls(),
                        evidence(**{field: False}),
                    )

    def test_operator_and_order_switch_controls_are_mandatory(self):
        cases = (
            {"T100_LIVE_ORDER_API_ENABLED": "false"},
            {"T100_LIVE_KILL_SWITCH": "true"},
            {"T100_LIVE_OPERATOR_CONFIRM": ""},
        )
        for case in cases:
            with self.subTest(case=case):
                with self.assertRaises(LiveAuthorizationHalted):
                    issue_live_mutation_permit(
                        controls(**case),
                        evidence(),
                    )

    def test_missing_risk_limit_blocks(self):
        with self.assertRaises(LiveAuthorizationHalted):
            issue_live_mutation_permit(
                controls(T100_LIVE_MAX_POSITION_NOTIONAL_USDT=""),
                evidence(),
            )

    def test_direct_permit_construction_is_blocked(self):
        with self.assertRaisesRegex(
            LiveAuthorizationHalted,
            "MUST_BE_ISSUED_BY_POLICY",
        ):
            LiveMutationPermit(
                max_position_notional_usdt=D("100"),
                max_total_notional_usdt=D("100"),
                max_daily_loss_usdt=D("25"),
                leverage=1,
                scope="T100_TINY_PILOT",
                _sentinel=object(),
            )

    def test_require_rejects_none_or_other_objects(self):
        for bad in (None, object(), "permit"):
            with self.subTest(bad=bad):
                with self.assertRaisesRegex(
                    LiveAuthorizationHalted,
                    "PERMIT_REQUIRED",
                ):
                    require_live_mutation_permit(bad)


if __name__ == "__main__":
    unittest.main()
