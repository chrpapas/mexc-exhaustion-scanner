"""Offline single-view pilot readiness gate tests."""
import unittest

from app.t100_live_pilot_readiness import (
    MIN_PROTECTED_SHADOW_SOAK_SECONDS,
    PilotReadinessEvidence,
    assess_pilot_readiness,
)


def evidence(**kw):
    data=dict(
        protected_shadow_soak_seconds=MIN_PROTECTED_SHADOW_SOAK_SECONDS,
        current_inventory_completeness_verified=True,
        account_reconciled=True,
        durable_schema_applied=True,
        append_only_permissions_verified=True,
        leader_fencing_verified=True,
        symbol_execution_eligible=True,
        no_unresolved_order_intents=True,
        no_unresolved_stop_amendments=True,
        protected_entry_recovery_tested=True,
        stop_amendment_recovery_tested=True,
        closed_position_accounting_tested=True,
        v40_admission_policy_tested=True,
        pilot_sizing_boundary_tested=True,
        discord_alerting_verified=True,
        explicit_emergency_procedure_reviewed=True,
        independent_review_complete=True,
    )
    data.update(kw)
    return PilotReadinessEvidence(**data)


class PilotReadinessTests(unittest.TestCase):
    def test_current_repo_stays_blocked_by_adapter_status(self):
        r=assess_pilot_readiness(evidence())
        self.assertFalse(r["ready_for_operator_authorization"])
        self.assertFalse(r["live_authorization"])
        self.assertIn(
            "live_adapter_status_not_implemented",
            r["blockers"],
        )

    def test_shadow_soak_requires_full_24h(self):
        r=assess_pilot_readiness(evidence(
            protected_shadow_soak_seconds=MIN_PROTECTED_SHADOW_SOAK_SECONDS-1
        ))
        self.assertIn("protected_shadow_soak_lt_24h",r["blockers"])

    def test_each_boolean_gate_fails_closed(self):
        fields=(
            "current_inventory_completeness_verified",
            "account_reconciled",
            "durable_schema_applied",
            "append_only_permissions_verified",
            "leader_fencing_verified",
            "symbol_execution_eligible",
            "no_unresolved_order_intents",
            "no_unresolved_stop_amendments",
            "protected_entry_recovery_tested",
            "stop_amendment_recovery_tested",
            "closed_position_accounting_tested",
            "v40_admission_policy_tested",
            "pilot_sizing_boundary_tested",
            "discord_alerting_verified",
            "explicit_emergency_procedure_reviewed",
            "independent_review_complete",
        )
        for field in fields:
            with self.subTest(field=field):
                r=assess_pilot_readiness(evidence(**{field:False}))
                self.assertFalse(r["ready_for_operator_authorization"])

    def test_invalid_evidence_fails_closed(self):
        r=assess_pilot_readiness(None)
        self.assertEqual(r["status"],"PILOT_READINESS_INPUT_INVALID")
        self.assertFalse(r["live_authorization"])


if __name__=="__main__":
    unittest.main()
