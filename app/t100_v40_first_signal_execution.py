"""Exactly one genuine V40 signal -> certified, fenced first-canary attempt.

This is the REAL coordinator integration, not a simulated paper fill. When
an independently issued V40 pilot permit exists, V40OneShotCoordinator owns
the durable reservation, irreversible claim, ONE MEXC POST with native SL75,
exact-origin GET fill reconciliation, and protected PostgreSQL commit.

This module deliberately does not self-certify signed risk, native stops,
independent guardian, emergency close or operator release. They must arrive
as typed evidence from other current verified sources. No alternate signal
or second POST is ever tried after a staged/unknown attempt.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime,timezone
from decimal import Decimal as D
from typing import Any

from app.t100_live_authorization import (
    V40FirstCanaryReadinessEvidence,issue_v40_supervised_canary_permit,
    LiveMutationPermit,require_certified_transport_permit,
)
from app.t100_live_execution import LiveCaps
from app.t100_live_guards import T100ExecutionControls
from app.t100_live_v40_entry_coordinator import (
    V40OneShotCoordinator,V40OneShotReport,prepare_candidate,
    verify_fresh_exact_depth,
)
from app.t100_live_four_slot_snapshot_coordinator import FourSlotSnapshotResult
from app.t100_live_v40_stage2_source import (
    PostgresV40Stage2Source,V40SourceCandidate,V40SourceBatch,
)
from app.t100_live_v40_candidate_market_witness import (
    MexcV40CandidateMarketWitness,
)
from app.t100_v40_first_canary_risk_witness import V40FirstCanaryRiskWitness
from app.t100_v40_executor_assembly import (
    assemble_v40_first_pilot,V40ExecutorAssembly,V40FirstPilotGetOnly,
)
from app.t100_v40_guardian_post_gate import (
    PostgresV40GuardianPostGate,V40GuardianPostGateEvidence,
)
from app.t100_live_mexc_adapter import LiveMexcAdapter
from app.t100_live_strategy_parity_gate import (
    assess_v40_first_canary_entry_parity_report,
)
from app.t100_v40_admission import VOL_SKIP_THRESHOLD

_GUARDIAN_OK="V40_GUARDIAN_POST_GATE_CURRENT_FLAT_UNLATCHED_NONAUTHORIZING"
_SOURCE_OK="FROZEN_V40_STAGE2_SOURCE_VALIDATED_NONAUTHORIZING"
_RELEASE_CAP=D("12.50")


@dataclass(frozen=True)
class V40FirstCanaryReleaseReview:
    """Externally reviewed proofs; constructor never certifies their truth."""
    code_revision_verified:bool=False
    native_attached_market_stop_request_contract_reviewed:bool=False
    post_fill_get_and_journal_reconciliation_reviewed:bool=False
    independent_nonflat_guardian_live_verified:bool=False
    durable_incident_alert_delivery_verified:bool=False
    supervised_emergency_exit_runbook_verified:bool=False
    operator_explicitly_authorized_first_canary:bool=False
    # True ONLY for the explicitly requested first production observation:
    # no claim of preverified native stop triggers or emergency exits.
    production_first_trade_observation_waiver:bool=False


@dataclass(frozen=True)
class V40FirstCanaryExecutionOutcome:
    status:str
    signal_id:int|None=None
    symbol:str|None=None
    candidate_prechecked:bool=False
    permit_issued:bool=False
    reservation_or_dispatch_may_exist:bool=False
    exchange_post_may_have_occurred:bool=False
    live_order_outcome_certified:bool=False
    further_entry_allowed:bool=False


def _guardian_verified(e:object)->bool:
    return (
        type(e) is V40GuardianPostGateEvidence
        and e.status==_GUARDIAN_OK
        and e.state_verified is True
        and e.outbox_verified is True
        and e.guardian_fresh is True
        and e.critical_latch_absent is True
        and e.entry_allowed is False
        and e.live_authorization is False
        and e.database_writes==0 and e.mexc_posts==0
    )


def _source_candidate(tape:object)->V40SourceCandidate|None:
    if (
        type(tape) is not V40SourceBatch
        or tape.status!=_SOURCE_OK
        or tape.read_only_repeatable_read is not True
        or tape.history_complete is not True
        or tape.entry_allowed is not False
        or tape.live_authorization is not False
        or type(tape.candidates) is not tuple
        or len(tape.candidates)>1
        or any(type(c) is not V40SourceCandidate for c in tape.candidates)
    ):
        raise ValueError("V40_FIRST_CANARY_GENUINE_SOURCE_INVALID")
    if not tape.candidates:
        return None
    signal=tape.candidates[0]
    if (
        signal.observed_age_seconds<0 or signal.observed_age_seconds>900
        or signal.entry_allowed is not False or signal.live_authorization is not False
        or signal.tier not in ("LOW_100","HIGH_225")
        or type(signal.signal_id) is not int or signal.signal_id<=0
    ):
        raise ValueError("V40_FIRST_CANARY_STALE_OR_INVALID_SIGNAL")
    return signal


async def attempt_one_real_v40_first_signal(
    *,
    source:PostgresV40Stage2Source,
    executor_runtime_pool:Any,
    signed_get:V40FirstPilotGetOnly,
    public_get:Any,
    key_and_risk:V40FirstCanaryRiskWitness,
    guardian_gate:PostgresV40GuardianPostGate,
    client:Any,
    controls:T100ExecutionControls,
    parity_report:dict[str,Any],
    external_review:V40FirstCanaryReleaseReview,
    holder_id:str,
    observed_at:datetime|None=None,
    caps:LiveCaps|None=None,
)->V40FirstCanaryExecutionOutcome:
    """One irreversible attempt maximum; never retried on UNKNOWN or crash."""
    blocked=lambda code,**kw:V40FirstCanaryExecutionOutcome(code,**kw)
    if (
        type(source) is not PostgresV40Stage2Source
        or source.restricted_source_reader is not True
        or source.pool is None
        or executor_runtime_pool is None
        or source.pool is executor_runtime_pool
        or type(signed_get) is not V40FirstPilotGetOnly
        or type(key_and_risk) is not V40FirstCanaryRiskWitness
        or type(guardian_gate) is not PostgresV40GuardianPostGate
        or type(external_review) is not V40FirstCanaryReleaseReview
        or type(controls) is not T100ExecutionControls
        or type(parity_report) is not dict
        or type(holder_id) is not str or not holder_id.strip()
        or type(client).__name__!="MexcTradeClient"
    ):
        return blocked("V40_FIRST_CANARY_LIVE_DEPENDENCIES_INVALID")
    if (
        controls.mode!="live"
        or controls.leverage!=1
        or controls.readiness_failures()
        or key_and_risk.market.private._client is not signed_get
        or key_and_risk.market.public is not public_get
        or guardian_gate.pool is not executor_runtime_pool
    ):
        return blocked("V40_FIRST_CANARY_LIVE_CONFIG_OR_ACCOUNT_IDENTITY_HALT")
    if caps is None:
        caps=LiveCaps(
            max_order_usdt=_RELEASE_CAP,
            max_portfolio_usdt=_RELEASE_CAP,
            max_daily_loss_usdt=_RELEASE_CAP,
        )
    if (
        type(caps) is not LiveCaps
        or caps.max_order_usdt>_RELEASE_CAP
        or caps.max_portfolio_usdt>_RELEASE_CAP
        or caps.max_daily_loss_usdt>_RELEASE_CAP
        or caps.leverage!=1 or caps.margin_type!=2
    ):
        return blocked("V40_FIRST_CANARY_CAPS_INVALID")
    check=assess_v40_first_canary_entry_parity_report(parity_report)
    if check["first_canary_entry_parity_verified"] is not True:
        return blocked("V40_FIRST_CANARY_HISTORICAL_ENTRY_PARITY_UNVERIFIED")

    now=datetime.now(timezone.utc) if observed_at is None else observed_at
    if type(now) is not datetime or now.tzinfo is None:
        return blocked("V40_FIRST_CANARY_CLOCK_INVALID")
    try:
        tape=await source.latest_candidates(observed_at=now)
        signal=_source_candidate(tape)
    except Exception:
        return blocked("V40_FIRST_CANARY_SOURCE_FAILED_NO_POST")
    if signal is None:
        return blocked("V40_FIRST_CANARY_WAIT_FOR_GENUINE_STAGE2")
    identity={"signal_id":signal.signal_id,"symbol":signal.symbol}
    if signal.pre24_vol_pct>=VOL_SKIP_THRESHOLD:
        return blocked("V40_FIRST_CANARY_GENUINE_VOL30_SKIP",**identity)
    try:
        guardian=await guardian_gate.verify()
        if not _guardian_verified(guardian):
            return blocked("V40_FIRST_CANARY_GUARDIAN_NOT_READY",**identity)
        risk,terms=await key_and_risk.inspect(signal)
        # Build a strictly disabled assembly ONLY for preflight snapshots.
        # No exchange POST or SQL mutation is available here.
        getonly=assemble_v40_first_pilot(
            signal_reader_pool=source.pool,
            executor_runtime_pool=executor_runtime_pool,
            signed_get=signed_get,public_get=public_get,
        )
        pre=await getonly.coordinator.snapshots.inspect()
        if (
            type(pre) is not FourSlotSnapshotResult
            or pre.status!="FLAT_OBSERVED_NONAUTHORIZING"
            or pre.occupied_slots!=0
        ):
            return blocked("V40_FIRST_CANARY_PRE_ENTRY_ACCOUNT_NOT_FLAT",**identity)
        ceiling=min(
            caps.max_order_usdt,risk.current_account_equity_usdt*D(".25"),
            risk.available_margin_usdt,
        )
        planned=prepare_candidate(
            signal=signal,witness=pre,risk=risk,terms=terms,
            cost_bps=100.,pilot_notional_ceiling=ceiling,
        )
        if planned is None:
            return blocked("V40_FIRST_CANARY_FROZEN_ADMISSION_REJECTED",**identity)
        quote,depth=await key_and_risk.market.quote_exact_intent(
            signal,planned.intent,max_slippage_bps=caps.max_slippage_bps,
        )
        verify_fresh_exact_depth(
            signal=signal,intent=planned.intent,risk=risk,terms=terms,
            caps=caps,quote=quote,depth=depth,
            now_ms=int(datetime.now(timezone.utc).timestamp()*1000),
        )
        # Repeat the independently maintained guardian gate before issuing
        # a signal-bound permit. The adapter checks this AGAIN at final POST.
        if not _guardian_verified(await guardian_gate.verify()):
            return blocked("V40_FIRST_CANARY_FINAL_GUARDIAN_PRECHECK_HALT",**identity)
    except Exception:
        return blocked("V40_FIRST_CANARY_CURRENT_RISK_DEPTH_OR_ACCOUNT_HALT",**identity)

    evidence=V40FirstCanaryReadinessEvidence(
        code_revision_verified=external_review.code_revision_verified,
        frozen_strategy_parity_verified=True,
        immutable_journal_schema_and_grants_verified=True,
        genuine_fresh_one_signal_verified=True,
        current_exact_lot_and_depth_verified=True,
        dedicated_account_flat_and_hedge_verified=True,
        private_short_cross_one_x_verified=True,
        account_open_orders_and_unknown_claims_absent=True,
        native_attached_market_stop_request_contract_reviewed=(
            external_review.native_attached_market_stop_request_contract_reviewed
        ),
        post_fill_get_and_journal_reconciliation_reviewed=(
            external_review.post_fill_get_and_journal_reconciliation_reviewed
        ),
        independent_nonflat_guardian_live_verified=(
            external_review.independent_nonflat_guardian_live_verified
        ),
        durable_incident_alert_delivery_verified=(
            external_review.durable_incident_alert_delivery_verified
        ),
        supervised_emergency_exit_runbook_verified=(
            external_review.supervised_emergency_exit_runbook_verified
        ),
        operator_explicitly_authorized_first_canary=(
            external_review.operator_explicitly_authorized_first_canary
        ),
        production_first_trade_observation_waiver=(
            external_review.production_first_trade_observation_waiver
        ),
    )
    if evidence.failures():
        return blocked("V40_FIRST_CANARY_RELEASE_REVIEW_INCOMPLETE_NO_POST",**identity,
                       candidate_prechecked=True)
    try:
        permit=issue_v40_supervised_canary_permit(
            controls,evidence,signal_id=signal.signal_id,symbol=signal.symbol,
        )
        require_certified_transport_permit(permit)
        adapter=LiveMexcAdapter(client,guardian_gate=guardian_gate)
        assembly=assemble_v40_first_pilot(
            signal_reader_pool=source.pool,
            executor_runtime_pool=executor_runtime_pool,
            signed_get=signed_get,public_get=public_get,
            live_transport=adapter,permit=permit,
            risk_witness=key_and_risk,
        )
        if (
            type(assembly) is not V40ExecutorAssembly
            or assembly.production_post_adapter_connected is not True
            or type(assembly.coordinator) is not V40OneShotCoordinator
            or assembly.coordinator.initial_pilot_only is not True
        ):
            return blocked("V40_FIRST_CANARY_LIVE_ASSEMBLY_INVALID",**identity,
                           candidate_prechecked=True)
    except Exception:
        return blocked("V40_FIRST_CANARY_PERMIT_OR_ADAPTER_NOT_RELEASED",**identity,
                       candidate_prechecked=True)

    # From this point forward a durable reservation/claim/POST may exist.
    # Never interpret a crash or SQL timeout as proof that no order was sent.
    try:
        report=await assembly.coordinator.run_one(
            permit=permit,caps=caps,parity_report=parity_report,
            holder_id=holder_id,
            monitored_signal_id=signal.signal_id,
            monitored_symbol=signal.symbol,monitored_tier=signal.tier,
        )
        if type(report) is not V40OneShotReport:
            raise ValueError("V40_RESULT_TYPE_INVALID")
        return blocked(
            report.status,**identity,candidate_prechecked=True,
            permit_issued=True,
            reservation_or_dispatch_may_exist=(
                report.reserved or report.staged or report.claim_may_exist
            ),
            exchange_post_may_have_occurred=report.post_attempted,
            live_order_outcome_certified=(
                report.status=="V40_ONE_PROTECTED_ENTRY_RECOVERED_NOT_AUTONOMOUS"
                and report.independently_recovered_after_commit is True
            ),
        )
    except BaseException:
        return blocked(
            "V40_FIRST_CANARY_UNKNOWN_AFTER_ENTRY_BOUNDARY_RECONCILE_ONLY",
            **identity,candidate_prechecked=True,permit_issued=True,
            reservation_or_dispatch_may_exist=True,
            exchange_post_may_have_occurred=True,
        )
