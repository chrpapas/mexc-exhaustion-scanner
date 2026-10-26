"""Single fail-closed release-gate view for a supervised tiny live pilot.

This is evidence aggregation only. It cannot submit orders and cannot turn the
current adapter into a certified live adapter.

Two distinct outcomes are reported:
- ready_for_operator_authorization: all technical/operational evidence is green
  and the system may ask for a separate explicit pilot authorization.
- live_authorization: remains false here by design. Authorization is issued only
  by the separate mutation-permit boundary after explicit approval.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from app.t100_live_guards import LIVE_ADAPTER_STATUS


@dataclass(frozen=True)
class PilotReadinessEvidence:
    protected_shadow_soak_seconds: int
    current_inventory_completeness_verified: bool
    account_reconciled: bool
    durable_schema_applied: bool
    append_only_permissions_verified: bool
    leader_fencing_verified: bool
    symbol_execution_eligible: bool
    clock_skew_verified: bool
    no_unresolved_order_intents: bool
    no_unresolved_stop_amendments: bool
    protected_entry_recovery_tested: bool
    stop_amendment_recovery_tested: bool
    closed_position_accounting_tested: bool
    v40_admission_policy_tested: bool
    pilot_sizing_boundary_tested: bool
    discord_alerting_verified: bool
    explicit_emergency_procedure_reviewed: bool
    independent_review_complete: bool


MIN_PROTECTED_SHADOW_SOAK_SECONDS = 24 * 60 * 60


def assess_pilot_readiness(
    evidence: PilotReadinessEvidence,
) -> dict[str, Any]:
    if not isinstance(evidence, PilotReadinessEvidence):
        return {
            "status": "PILOT_READINESS_INPUT_INVALID",
            "ready_for_operator_authorization": False,
            "live_authorization": False,
            "blockers": ["invalid_evidence"],
        }

    blockers: list[str] = []
    if LIVE_ADAPTER_STATUS != "CERTIFIED":
        blockers.append(
            f"live_adapter_status_{LIVE_ADAPTER_STATUS.lower()}"
        )
    if (
        isinstance(evidence.protected_shadow_soak_seconds, bool)
        or not isinstance(evidence.protected_shadow_soak_seconds, int)
        or evidence.protected_shadow_soak_seconds
        < MIN_PROTECTED_SHADOW_SOAK_SECONDS
    ):
        blockers.append("protected_shadow_soak_lt_24h")

    gates = (
        (
            "current_inventory_completeness_unverified",
            evidence.current_inventory_completeness_verified,
        ),
        ("account_not_reconciled", evidence.account_reconciled),
        ("durable_schema_not_applied", evidence.durable_schema_applied),
        (
            "append_only_permissions_unverified",
            evidence.append_only_permissions_verified,
        ),
        ("leader_fencing_unverified", evidence.leader_fencing_verified),
        (
            "symbol_execution_ineligible",
            evidence.symbol_execution_eligible,
        ),
        (
            "clock_skew_unverified",
            evidence.clock_skew_verified,
        ),
        (
            "unresolved_order_intents_present",
            evidence.no_unresolved_order_intents,
        ),
        (
            "unresolved_stop_amendments_present",
            evidence.no_unresolved_stop_amendments,
        ),
        (
            "protected_entry_recovery_untested",
            evidence.protected_entry_recovery_tested,
        ),
        (
            "stop_amendment_recovery_untested",
            evidence.stop_amendment_recovery_tested,
        ),
        (
            "closed_position_accounting_untested",
            evidence.closed_position_accounting_tested,
        ),
        (
            "v40_admission_policy_untested",
            evidence.v40_admission_policy_tested,
        ),
        (
            "pilot_sizing_boundary_untested",
            evidence.pilot_sizing_boundary_tested,
        ),
        (
            "discord_alerting_unverified",
            evidence.discord_alerting_verified,
        ),
        (
            "emergency_procedure_unreviewed",
            evidence.explicit_emergency_procedure_reviewed,
        ),
        (
            "independent_review_incomplete",
            evidence.independent_review_complete,
        ),
    )
    for blocker, passed in gates:
        if passed is not True:
            blockers.append(blocker)

    return {
        "status": (
            "TECHNICAL_GATES_READY_FOR_EXPLICIT_OPERATOR_AUTHORIZATION"
            if not blockers
            else "PILOT_BLOCKED"
        ),
        "protected_shadow_soak_seconds": (
            evidence.protected_shadow_soak_seconds
        ),
        "minimum_shadow_soak_seconds": (
            MIN_PROTECTED_SHADOW_SOAK_SECONDS
        ),
        "blockers": blockers,
        "ready_for_operator_authorization": not blockers,
        "live_authorization": False,
    }
