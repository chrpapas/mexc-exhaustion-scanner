"""Final fail-closed mutation permit boundary for future T100 live execution.

Healthy diagnostics, operator configuration, or a valid leader lease are not
sufficient. A permit can exist only when:
- the live adapter status is explicitly CERTIFIED,
- operator arming controls pass,
- the operational kill switch is not engaged,
- a current leader fencing lease is verified,
- account reconciliation/current inventory completeness are verified,
- no unresolved order/stop mutations remain,
- the protected shadow soak gate has passed,
- the durable live schema has been applied and append-only permissions verified,
- explicit pilot authorization is present.

Current repository state keeps LIVE_ADAPTER_STATUS=NOT_IMPLEMENTED, therefore
this issuer cannot currently produce a permit.
"""
from __future__ import annotations

from dataclasses import dataclass

from app.t100_live_guards import (
    LIVE_ADAPTER_STATUS,
    T100ExecutionControls,
)
from app.t100_live_leader_fencing import (
    LeaderLease,
    LeaderLeaseCoordinator,
    LeaderLeaseHalted,
)


class LiveMutationPermitHalted(RuntimeError):
    pass


@dataclass(frozen=True)
class LiveMutationEvidence:
    account_reconciled: bool
    current_inventory_complete: bool
    no_unresolved_order_intents: bool
    no_unresolved_stop_amendments: bool
    protected_shadow_soak_passed: bool
    durable_schema_applied: bool
    append_only_permissions_verified: bool
    explicit_pilot_authorization: bool


@dataclass(frozen=True)
class LiveMutationPermit:
    holder_id: str
    fencing_token: int
    max_position_notional_usdt: str
    max_total_notional_usdt: str
    max_daily_loss_usdt: str
    leverage: int


class LiveMutationPermitIssuer:
    def __init__(
        self,
        *,
        controls: T100ExecutionControls,
        lease_coordinator: LeaderLeaseCoordinator,
    ) -> None:
        if not isinstance(controls, T100ExecutionControls):
            raise ValueError("INVALID_EXECUTION_CONTROLS")
        if not isinstance(lease_coordinator, LeaderLeaseCoordinator):
            raise ValueError("INVALID_LEASE_COORDINATOR")
        self.controls = controls
        self.leases = lease_coordinator

    async def issue(
        self,
        *,
        lease: LeaderLease,
        evidence: LiveMutationEvidence,
    ) -> LiveMutationPermit:
        if LIVE_ADAPTER_STATUS != "CERTIFIED":
            raise LiveMutationPermitHalted(
                f"LIVE_ADAPTER_NOT_CERTIFIED:{LIVE_ADAPTER_STATUS}"
            )
        if self.controls.mode != "live":
            raise LiveMutationPermitHalted("LIVE_MODE_NOT_ENABLED")
        failures = self.controls.readiness_failures()
        if failures:
            raise LiveMutationPermitHalted(
                "LIVE_OPERATOR_CONTROLS_BLOCKED:" + ",".join(failures)
            )
        if self.controls.kill_switch_engaged:
            raise LiveMutationPermitHalted("LIVE_KILL_SWITCH_ENGAGED")

        if not isinstance(evidence, LiveMutationEvidence):
            raise LiveMutationPermitHalted("LIVE_EVIDENCE_INVALID")
        required = (
            evidence.account_reconciled,
            evidence.current_inventory_complete,
            evidence.no_unresolved_order_intents,
            evidence.no_unresolved_stop_amendments,
            evidence.protected_shadow_soak_passed,
            evidence.durable_schema_applied,
            evidence.append_only_permissions_verified,
            evidence.explicit_pilot_authorization,
        )
        if any(value is not True for value in required):
            raise LiveMutationPermitHalted(
                "LIVE_MUTATION_EVIDENCE_INCOMPLETE"
            )

        try:
            await self.leases.require_current(lease)
        except LeaderLeaseHalted as exc:
            raise LiveMutationPermitHalted(
                "LIVE_LEADER_FENCE_INVALID"
            ) from exc

        limits = (
            self.controls.max_position_notional_usdt,
            self.controls.max_total_notional_usdt,
            self.controls.max_daily_loss_usdt,
        )
        if any(x is None for x in limits):
            raise LiveMutationPermitHalted(
                "LIVE_RISK_LIMITS_INCOMPLETE"
            )

        return LiveMutationPermit(
            holder_id=lease.holder_id,
            fencing_token=lease.fencing_token,
            max_position_notional_usdt=str(limits[0]),
            max_total_notional_usdt=str(limits[1]),
            max_daily_loss_usdt=str(limits[2]),
            leverage=self.controls.leverage,
        )
