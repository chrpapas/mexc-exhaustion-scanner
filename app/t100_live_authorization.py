"""Typed fail-closed authorization boundary for T100 exchange mutations.

This is an accidental-use safety boundary, not a cryptographic capability.
A permit can be issued only from explicit live controls plus a complete set
of independently supplied release-gate evidence. Paper mode can never issue
a permit.

Issuing a permit does NOT submit an order and does not make any exchange call.
"""
from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

from app import t100_live_guards
from app.t100_live_guards import T100ExecutionControls


class LiveAuthorizationHalted(RuntimeError):
    pass


@dataclass(frozen=True)
class LivePilotReadinessEvidence:
    code_revision_verified: bool
    strategy_parity_verified: bool
    durable_schema_deployed_verified: bool
    current_inventory_completeness_verified: bool
    account_reconciled: bool
    protected_position_soak_verified: bool
    restart_recovery_verified: bool
    native_stop_mutation_contract_verified: bool
    entry_reconciliation_verified: bool
    fill_and_accounting_verified: bool
    private_ws_pre_pilot_contract_verified: bool
    unresolved_mutations_absent: bool
    independent_review_completed: bool
    operator_tiny_pilot_authorized: bool

    def failures(self) -> tuple[str, ...]:
        mapping = (
            ("code_revision_unverified", self.code_revision_verified),
            ("strategy_parity_unverified", self.strategy_parity_verified),
            ("durable_schema_not_deployed", self.durable_schema_deployed_verified),
            (
                "current_inventory_completeness_unverified",
                self.current_inventory_completeness_verified,
            ),
            ("account_not_reconciled", self.account_reconciled),
            (
                "protected_position_soak_unverified",
                self.protected_position_soak_verified,
            ),
            ("restart_recovery_unverified", self.restart_recovery_verified),
            (
                "native_stop_mutation_contract_unverified",
                self.native_stop_mutation_contract_verified,
            ),
            (
                "entry_reconciliation_unverified",
                self.entry_reconciliation_verified,
            ),
            (
                "fill_and_accounting_unverified",
                self.fill_and_accounting_verified,
            ),
            (
                "private_ws_pre_pilot_contract_unverified",
                self.private_ws_pre_pilot_contract_verified,
            ),
            (
                "unresolved_mutations_present",
                self.unresolved_mutations_absent,
            ),
            (
                "independent_review_incomplete",
                self.independent_review_completed,
            ),
            (
                "operator_tiny_pilot_authorization_missing",
                self.operator_tiny_pilot_authorized,
            ),
        )
        return tuple(name for name, ok in mapping if ok is not True)


_PERMIT_SENTINEL = object()


@dataclass(frozen=True, init=False)
class LiveMutationPermit:
    max_position_notional_usdt: Decimal
    max_total_notional_usdt: Decimal
    max_daily_loss_usdt: Decimal
    leverage: int
    scope: str

    def __init__(
        self,
        *,
        max_position_notional_usdt: Decimal,
        max_total_notional_usdt: Decimal,
        max_daily_loss_usdt: Decimal,
        leverage: int,
        scope: str,
        _sentinel: object,
    ) -> None:
        if _sentinel is not _PERMIT_SENTINEL:
            raise LiveAuthorizationHalted(
                "LIVE_MUTATION_PERMIT_MUST_BE_ISSUED_BY_POLICY"
            )
        object.__setattr__(
            self,
            "max_position_notional_usdt",
            max_position_notional_usdt,
        )
        object.__setattr__(
            self,
            "max_total_notional_usdt",
            max_total_notional_usdt,
        )
        object.__setattr__(
            self,
            "max_daily_loss_usdt",
            max_daily_loss_usdt,
        )
        object.__setattr__(self, "leverage", leverage)
        object.__setattr__(self, "scope", scope)


def issue_live_mutation_permit(
    controls: T100ExecutionControls,
    evidence: LivePilotReadinessEvidence,
) -> LiveMutationPermit:
    if not isinstance(controls, T100ExecutionControls):
        raise LiveAuthorizationHalted("LIVE_CONTROLS_INVALID")
    if not isinstance(evidence, LivePilotReadinessEvidence):
        raise LiveAuthorizationHalted("LIVE_READINESS_EVIDENCE_INVALID")

    failures: list[str] = []
    if controls.mode != "live":
        failures.append("execution_mode_not_live")
    failures.extend(controls.readiness_failures())
    failures.extend(evidence.failures())

    if controls.leverage != 1:
        failures.append("tiny_pilot_requires_1x_leverage")
    if (
        controls.max_position_notional_usdt is None
        or controls.max_total_notional_usdt is None
        or controls.max_daily_loss_usdt is None
    ):
        # readiness_failures already reports these, but keep construction
        # impossible even if that method changes later.
        failures.append("live_risk_limits_incomplete")

    if failures:
        raise LiveAuthorizationHalted(
            "LIVE_MUTATION_AUTHORIZATION_BLOCKED:"
            + ",".join(dict.fromkeys(failures))
        )

    return LiveMutationPermit(
        max_position_notional_usdt=controls.max_position_notional_usdt,
        max_total_notional_usdt=controls.max_total_notional_usdt,
        max_daily_loss_usdt=controls.max_daily_loss_usdt,
        leverage=controls.leverage,
        scope="T100_TINY_PILOT",
        _sentinel=_PERMIT_SENTINEL,
    )


def require_live_mutation_permit(
    permit: LiveMutationPermit,
) -> LiveMutationPermit:
    if not isinstance(permit, LiveMutationPermit):
        raise LiveAuthorizationHalted("LIVE_MUTATION_PERMIT_REQUIRED")
    if (
        permit.scope != "T100_TINY_PILOT"
        or permit.leverage != 1
        or permit.max_position_notional_usdt <= 0
        or permit.max_total_notional_usdt <= 0
        or permit.max_daily_loss_usdt <= 0
        or permit.max_position_notional_usdt
        > permit.max_total_notional_usdt
    ):
        raise LiveAuthorizationHalted("LIVE_MUTATION_PERMIT_INVALID")
    return permit


def require_certified_transport_permit(
    permit: LiveMutationPermit,
) -> LiveMutationPermit:
    """Guard the final transport boundary, independent of caller evidence.

    An offline test can simulate a certified adapter by patching the module
    constant, but a production adapter remains blocked while the repository's
    status is NOT_IMPLEMENTED. This does not certify the adapter or itself
    establish live permissions, leader fencing, or current account readiness.
    """
    checked = require_live_mutation_permit(permit)
    if t100_live_guards.LIVE_ADAPTER_STATUS != "CERTIFIED":
        raise LiveAuthorizationHalted("LIVE_ADAPTER_NOT_CERTIFIED")
    return checked
