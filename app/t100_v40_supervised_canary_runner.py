"""ONE supervised first V40 real-signal invocation, never a scheduler.

This is a callable boundary around the already-integrated V40OneShotCoordinator.
It is intentionally separate from the permanently paper/GET-only Render
monitor. It does not load credentials from environment, start a loop or
perform an order outside the authenticated single-signal coordinator.

The first trade is exactly one fresh, eligible Stage2 signal, matching the
operator-reviewed canary release; no BTC/ETH random pilot. All exchange POST
work still goes through immutable reservation/stage/claim/reconciliation and
native attached-stop verification. On unknown outcome it freezes and does
not retry. The release issuer does NOT require prior real exit soak—that is
the output of this supervised test, not a prerequisite.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime,timezone
from typing import Any,Protocol

from app.t100_live_authorization import (
    LiveAuthorizationHalted,V40FirstCanaryReadinessEvidence,
    issue_v40_supervised_canary_permit,require_certified_transport_permit,
)
from app.t100_live_execution import LiveCaps
from app.t100_live_guards import T100ExecutionControls
from app.t100_live_v40_entry_coordinator import (
    V40OneShotReport,V40OneShotCoordinator,
)
from app.t100_live_v40_stage2_source import (
    V40SourceBatch,V40SourceCandidate,
)


class V40CanaryHalted(RuntimeError):
    pass


@dataclass(frozen=True)
class V40CanaryOneShotOutcome:
    status: str
    signal_id: int|None=None
    real_post_may_have_been_attempted: bool=False
    independently_recovered_after_commit: bool=False
    additional_entries_allowed: bool=False
    automatic_retry_allowed: bool=False
    full_autonomous_mode: bool=False


class Source(Protocol):
    async def latest_candidates(self,*,observed_at:datetime)->V40SourceBatch: ...


async def execute_one_real_v40_signal(
    *,
    source:Source,coordinator:V40OneShotCoordinator,
    controls:T100ExecutionControls,
    evidence:V40FirstCanaryReadinessEvidence,
    caps:LiveCaps,parity_report:dict[str,Any],
    holder_id:str,
)->V40CanaryOneShotOutcome:
    """One bounded candidate; never auto-retry on a possible durable POST.

    The CLI/deployment must provide a trusted operator-reviewed evidence
    source and independently provisioned guardian before calling this.
    Neither a monitor heartbeat nor a passing CI suite mints a permit.
    """
    def outcome(s:str,**fields:Any)->V40CanaryOneShotOutcome:
        return V40CanaryOneShotOutcome(status=s,**fields)
    if (
        type(coordinator) is not V40OneShotCoordinator
        or coordinator.initial_pilot_only is not True
        or type(caps) is not LiveCaps
        or caps.margin_type!=2 or caps.leverage!=1
        or caps.max_order_usdt>12.5
        or caps.max_portfolio_usdt>12.5
        or caps.max_daily_loss_usdt>12.5
        or type(holder_id) is not str or not holder_id
        or type(parity_report) is not dict
    ):
        return outcome("V40_CANARY_COORDINATOR_OR_PILOT_CAP_INVALID_NO_POST")
    if type(controls) is not T100ExecutionControls or (
        type(evidence) is not V40FirstCanaryReadinessEvidence
    ):
        return outcome("V40_CANARY_OPERATOR_OR_RELEASE_EVIDENCE_MISSING_NO_POST")
    # The operator live controls must pass before even reading the scanner.
    if controls.mode!="live" or controls.readiness_failures():
        return outcome("V40_CANARY_OPERATOR_LIVE_CONTROLS_OFF_NO_POST")
    try:
        tape=await source.latest_candidates(observed_at=datetime.now(timezone.utc))
    except Exception:
        return outcome("V40_CANARY_RESTRICTED_STAGE2_UNAVAILABLE_NO_POST")
    if (
        type(tape) is not V40SourceBatch
        or tape.status!="FROZEN_V40_STAGE2_SOURCE_VALIDATED_NONAUTHORIZING"
        or tape.read_only_repeatable_read is not True
        or tape.entry_allowed is not False
        or tape.live_authorization is not False
        or len(tape.candidates)!=1
        or type(tape.candidates[0]) is not V40SourceCandidate
        or not 0<=tape.candidates[0].observed_age_seconds<=900
    ):
        return outcome("V40_CANARY_NO_SINGLE_FRESH_ELIGIBLE_STAGE2_NO_POST")
    signal=tape.candidates[0]
    try:
        permit=issue_v40_supervised_canary_permit(
            controls,evidence,signal_id=signal.signal_id,symbol=signal.symbol,
        )
        require_certified_transport_permit(permit)
    except LiveAuthorizationHalted:
        return outcome(
            "V40_CANARY_INDEPENDENT_RELEASE_OR_ADAPTER_NOT_READY_NO_POST",
            signal_id=signal.signal_id,
        )
    try:
        report=await coordinator.run_one(
            permit=permit,caps=caps,parity_report=parity_report,
            holder_id=holder_id,
            monitored_signal_id=signal.signal_id,
            monitored_symbol=signal.symbol,
            monitored_tier=signal.tier,
        )
    except BaseException:
        # Any exception after coordinator enters its fenced SQL path may
        # follow an irreversible claim/real POST. Never infer no fill.
        return outcome(
            "V40_CANARY_EXECUTION_OUTCOME_UNKNOWN_MANUAL_RECONCILIATION",
            signal_id=signal.signal_id,real_post_may_have_been_attempted=True,
        )
    if type(report) is not V40OneShotReport:
        return outcome(
            "V40_CANARY_REPORT_INVALID_MANUAL_RECONCILIATION",
            signal_id=signal.signal_id,real_post_may_have_been_attempted=True,
        )
    return outcome(
        report.status,
        signal_id=report.signal_id,
        real_post_may_have_been_attempted=(
            report.post_attempted or report.claim_may_exist
        ),
        independently_recovered_after_commit=(
            report.independently_recovered_after_commit
        ),
    )
