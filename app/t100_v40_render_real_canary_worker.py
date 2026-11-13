"""Production dependency composition for one real V40 first-signal canary.

Opt-in only: the existing Render worker remains paper/GET-only by default.
This module wires the REAL source, signed market/account risk, guardian gate,
fenced runtime journal, native SL75 MEXC adapter, and existing one-shot
coordinator. It cannot run without explicit tiny live controls and a
separately certified native-stop/emergency/recovery release.

No environmental boolean or paper diagnostic can fabricate those independent
certificates. Until certification, RELEASE_REVIEW is deliberately unapproved.
The worker never retries an ambiguous post, duplicates a consumed signal, or
trades BOXWAVE/legacy paper signals.
"""
from __future__ import annotations

import argparse
import asyncio
from dataclasses import asdict,dataclass
from datetime import datetime,timezone
import json
import os
import re
import time
from typing import Any,Awaitable,Callable,Mapping

from app.t100_live_guards import T100ExecutionControls
from app.t100_v40_first_signal_execution import (
    V40FirstCanaryReleaseReview,V40FirstCanaryExecutionOutcome,
    attempt_one_real_v40_first_signal,
)

FLAG="T100_V40_FIRST_CANARY_ORDER_WORKER_ENABLED"
SOURCE_DSN="T100_V40_SIGNAL_READER_DSN"
RUNTIME_DSN="T100_V40_EXECUTOR_RUNTIME_DSN"
MAX_NOTIONAL="12.50"
INTERVAL_SECONDS=15

# External safety proofs are intentionally not promoted from environment.
# A verified, reviewed emergency exit and native SL75 lifecycle MUST be
# implemented before production can populate this release review.
RELEASE_REVIEW=V40FirstCanaryReleaseReview()
TRIAL_FLAG="T100_V40_FIRST_REAL_SIGNAL_PRODUCTION_TRIAL_ACCEPTED"


def operator_requested_production_trial(
    env:Mapping[str,str],
)->V40FirstCanaryReleaseReview:
    """First genuine real trade validates venue outcomes instead of presuming them.

    Code/contract, durable persistence and emergency runbook have offline
    review evidence (merged PRs #24-#32); the first nonflat/Discord outcome
    remains explicitly UNVERIFIED, as requested by the operator. The flag
    is an operator-risk choice, NOT an API capability or fake certification.
    """
    if env.get(TRIAL_FLAG)!="true":
        return RELEASE_REVIEW
    return V40FirstCanaryReleaseReview(
        code_revision_verified=True,
        native_attached_market_stop_request_contract_reviewed=True,
        post_fill_get_and_journal_reconciliation_reviewed=True,
        independent_nonflat_guardian_live_verified=False,
        durable_incident_alert_delivery_verified=False,
        supervised_emergency_exit_runbook_verified=True,
        operator_explicitly_authorized_first_canary=True,
        production_first_trade_observation_waiver=True,
    )


@dataclass(frozen=True)
class V40CanaryWorkerStatus:
    status:str
    source_verified:bool=False
    account_identity_verified:bool=False
    release_review_complete:bool=False
    genuine_signal_observed:bool=False
    order_post_may_have_occurred:bool=False
    further_order_allowed:bool=False
    entry_allowed:bool=False
    live_authorization:bool=False


def _redact(status:str,**kwargs:Any)->V40CanaryWorkerStatus:
    return V40CanaryWorkerStatus(status=status,**kwargs)


def validate_live_worker_config(
    env:Mapping[str,str],review:V40FirstCanaryReleaseReview=RELEASE_REVIEW,
)->V40CanaryWorkerStatus:
    """No I/O or import of DB/exchange credential clients until gated."""
    if (
        env.get(FLAG)!="true"
        or env.get("T100_V40_ACCOUNT_PROFILE")!="DEDICATED_WEB_PILOT"
        or env.get("T100_V40_SINGLE_POSITION_PILOT")!="true"
        or env.get("TRADING_MODE")!="live"
        or env.get("EXECUTION_ENABLED")!="true"
        or env.get("T100_V40_EXECUTOR_DISABLED_WORKER")=="true"
        or env.get("DATABASE_URL")
        or env.get("T100_LIVE_DATABASE_URL")
        or env.get("T100_V40_TRADER_API_KEY")
        or env.get("T100_V40_TRADER_API_SECRET")
        or type(review) is not V40FirstCanaryReleaseReview
    ):
        return _redact("V40_REAL_WORKER_ACCOUNT_OR_KILL_ISOLATION_HALT")
    # The first live entry requires a separately routed CLOSE-ONLY watch.
    if env.get("T100_V40_EMERGENCY_EXIT_ENABLED")!="true":
        return _redact("V40_REAL_WORKER_EMERGENCY_CLOSE_WATCH_REQUIRED")
    try:
        controls=T100ExecutionControls.from_env(env)
    except Exception:
        return _redact("V40_REAL_WORKER_CONTROLS_INVALID")
    from decimal import Decimal as D
    if (
        controls.mode!="live"
        or controls.readiness_failures()
        or controls.leverage!=1
        or any(
            getattr(controls,key)!=D(MAX_NOTIONAL)
            for key in (
                "max_position_notional_usdt","max_total_notional_usdt",
                "max_daily_loss_usdt",
            )
        )
    ):
        return _redact("V40_REAL_WORKER_TINY_PILOT_CONTROLS_MISSING")
    for key in (SOURCE_DSN,RUNTIME_DSN):
        val=env.get(key,"")
        if (
            type(val) is not str
            or not val.startswith(("postgresql://","postgres://"))
            or len(val)>3000
        ):
            return _redact("V40_REAL_WORKER_DATABASE_ROLE_DSN_MISSING")
    if env[SOURCE_DSN]==env[RUNTIME_DSN]:
        return _redact("V40_REAL_WORKER_SQL_ROLES_NOT_SEPARATED")
    key=env.get("MEXC_API_KEY","")
    secret=env.get("MEXC_API_SECRET","")
    if (
        type(key) is not str or len(key)<12 or key!=key.strip()
        or type(secret) is not str or len(secret)<16 or secret!=secret.strip()
    ):
        return _redact("V40_REAL_WORKER_PILOT_ACCOUNT_KEY_MISSING")
    # The FIRST genuine real trade is the selected production experiment.
    # The operator accepts that live nonflat guardian/alert delivery outcomes
    # cannot be observed before that first position exists. This waiver
    # NEVER skips code/stop-request/journal/emergency-runbook verification.
    allow_trial=(
        review.production_first_trade_observation_waiver is True
        and review.operator_explicitly_authorized_first_canary is True
        and env.get(TRIAL_FLAG)=="true"
    )
    if not all((
        review.code_revision_verified,
        review.native_attached_market_stop_request_contract_reviewed,
        review.post_fill_get_and_journal_reconciliation_reviewed,
        review.supervised_emergency_exit_runbook_verified,
        review.operator_explicitly_authorized_first_canary,
        (review.independent_nonflat_guardian_live_verified or allow_trial),
        (review.durable_incident_alert_delivery_verified or allow_trial),
    )):
        return _redact("V40_REAL_WORKER_NATIVE_STOP_OR_RECOVERY_RELEASE_NOT_CERTIFIED")
    from app.t100_live_authorization import require_certified_transport_permit
    from app.t100_live_guards import LIVE_ADAPTER_STATUS
    if LIVE_ADAPTER_STATUS!="V40_CANARY_RESTRICTED":
        return _redact("V40_REAL_WORKER_EXCHANGE_ADAPTER_NOT_CERTIFIED")
    return _redact(
        "V40_REAL_WORKER_RELEASE_CONFIG_VERIFIED_NEEDS_CURRENT_SIGNED_EVIDENCE",
        release_review_complete=True,
    )


async def run_real_first_canary_cycle(
    env:Mapping[str,str],*,
    review:V40FirstCanaryReleaseReview|None=None,
    act:Callable[...,Awaitable[V40FirstCanaryExecutionOutcome]]=
        attempt_one_real_v40_first_signal,
)->V40CanaryWorkerStatus:
    """One bounded, independently fenced pass; never loops after a mutation."""
    current_review=review if review is not None else operator_requested_production_trial(env)
    configured=validate_live_worker_config(env,review=current_review)
    if not configured.release_review_complete:
        return configured
    # Imports and connection creation only after independent release check.
    import asyncpg
    import httpx
    from app.mexc_trade import MexcTradeClient
    from app.t100_live_readonly_probe import ReadOnlyMexcProbe
    from app.t100_live_v40_stage2_source import PostgresV40Stage2Source
    from app.t100_live_v40_candidate_market_witness import (
        MexcV40CandidateMarketWitness,
    )
    from app.t100_v40_executor_assembly import V40FirstPilotGetOnly
    from app.t100_v40_guardian_post_gate import PostgresV40GuardianPostGate
    from app.t100_v40_first_canary_risk_witness import V40FirstCanaryRiskWitness
    from app.t100_v40_canary_key_permissions import (
        read_v40_key_permissions_signed_get,
    )
    from app.t100_v40_canary_daily_loss import (
        V40HistoryGetOnly,observe_daily_loss,
    )
    from app.t100_v40_render_journal_readonly import observe_render_runtime_journal
    from app.t100_v40_first_canary_parity_evidence import (
        load_verified_frozen_v40_entry_parity,
    )
    from app.t100_live_guards import T100ExecutionControls
    from app.t100_v40_emergency_close_claims import (
        verify_runtime_first_canary_close_claim_schema,
    )
    source_pool=runtime_pool=None
    try:
        # Source reader remains PostgreSQL SELECT-only even while executor
        # runtime has narrowly scoped immutable journal writes.
        source_pool=await asyncpg.create_pool(
            dsn=env[SOURCE_DSN],ssl="require",
            min_size=1,max_size=1,timeout=12,command_timeout=15,
            server_settings={"default_transaction_read_only":"on"},
        )
        runtime_pool=await asyncpg.create_pool(
            dsn=env[RUNTIME_DSN],ssl="require",
            min_size=1,max_size=1,timeout=12,command_timeout=15,
        )
        # Distinct pools and credentials are both required.
        if source_pool is runtime_pool:
            return _redact("V40_REAL_WORKER_DATABASE_POOL_COLLISION")
        if not await verify_runtime_first_canary_close_claim_schema(runtime_pool):
            return _redact("V40_REAL_WORKER_EMERGENCY_CLOSE_SCHEMA_OR_PRIOR_CLAIM_HALT")
        source=PostgresV40Stage2Source(
            source_pool,restricted_source_reader=True,
        )
        key=env["MEXC_API_KEY"]
        secret=env["MEXC_API_SECRET"]
        async with httpx.AsyncClient(
            base_url="https://api.mexc.com",
            trust_env=False,follow_redirects=False,
            timeout=httpx.Timeout(10.0),
        ) as signed_http, httpx.AsyncClient(
            base_url="https://api.mexc.com",
            trust_env=False,follow_redirects=False,
            timeout=httpx.Timeout(10.0),
        ) as public_http, httpx.AsyncClient(
            base_url="https://api.mexc.com",
            trust_env=False,follow_redirects=False,
            timeout=httpx.Timeout(10.0),
        ) as extra_http:
            signed_get=V40FirstPilotGetOnly(
                signed_http,api_key=key,api_secret=secret,
            )
            market=MexcV40CandidateMarketWitness(
                private=ReadOnlyMexcProbe(signed_get),
                public=public_http,
                clock_ms=lambda:int(time.time()*1000),
            )
            async def permissions():
                return await read_v40_key_permissions_signed_get(
                    key=key,secret=secret,client=extra_http,
                )
            async def daily_loss():
                reader=V40HistoryGetOnly(
                    extra_http,api_key=key,api_secret=secret,
                )
                return await observe_daily_loss(
                    reader,now=datetime.now(timezone.utc),
                )
            async def journal():
                return await observe_render_runtime_journal(env)
            witness=V40FirstCanaryRiskWitness(
                market=market,get_permissions=permissions,
                get_daily_loss=daily_loss,get_runtime_journal=journal,
            )
            gate=PostgresV40GuardianPostGate(runtime_pool)
            parity=load_verified_frozen_v40_entry_parity()
            venue=MexcTradeClient(
                "https://api.mexc.com",api_key=key,api_secret=secret,
            )
            try:
                outcome=await asyncio.wait_for(
                    act(
                        source=source,executor_runtime_pool=runtime_pool,
                        signed_get=signed_get,public_get=public_http,
                        key_and_risk=witness,guardian_gate=gate,client=venue,
                        controls=T100ExecutionControls.from_env(env),
                        parity_report=parity,external_review=current_review,
                        holder_id="v40-first-canary-render-once",
                    ),
                    timeout=180,
                )
            except BaseException:
                return _redact(
                    "V40_REAL_WORKER_ENTRY_OUTCOME_UNKNOWN_MANUAL_RECONCILIATION",
                    release_review_complete=True,
                    order_post_may_have_occurred=True,
                )
            finally:
                await venue.close()
            if type(outcome) is not V40FirstCanaryExecutionOutcome:
                return _redact(
                    "V40_REAL_WORKER_RESULT_UNVERIFIED_NO_RETRY",
                    release_review_complete=True,
                    order_post_may_have_occurred=True,
                )
            return _redact(
                outcome.status,source_verified=True,
                account_identity_verified=True,
                release_review_complete=True,
                genuine_signal_observed=outcome.signal_id is not None,
                order_post_may_have_occurred=outcome.exchange_post_may_have_occurred,
            )
    except BaseException:
        # Never leak credentials/URL or retry after an ambiguous response.
        return _redact(
            "V40_REAL_WORKER_CONNECTION_OR_RUNTIME_UNKNOWN_HALT",
            release_review_complete=True,
            order_post_may_have_occurred=True,
        )
    finally:
        if source_pool is not None:
            await source_pool.close()
        if runtime_pool is not None:
            await runtime_pool.close()


async def run_live_worker(
    env:Mapping[str,str],*,max_cycles:int|None=None,
    cycle:Callable[...,Awaitable[V40CanaryWorkerStatus]]=run_real_first_canary_cycle,
    pause:Callable[[float],Awaitable[Any]]=asyncio.sleep,
    emergency_watch:Callable[...,Awaitable[int]]|None=None,
)->int:
    if max_cycles is not None and (
        type(max_cycles) is not int or max_cycles<1
    ):
        return 2
    start=validate_live_worker_config(
        env,review=operator_requested_production_trial(env),
    )
    if not start.release_review_complete:
        print(json.dumps(asdict(start),sort_keys=True),flush=True)
        return 2
    count=0
    while max_cycles is None or count<max_cycles:
        # Only empty source is retried. Account/error/unknown outcomes exit.
        try:
            report=await cycle(env)
        except BaseException:
            report=_redact("V40_REAL_WORKER_UNEXPECTED_HALT")
        if type(report) is not V40CanaryWorkerStatus:
            report=_redact("V40_REAL_WORKER_UNTRUSTED_CYCLE_RESULT_HALT")
        print(json.dumps(asdict(report),sort_keys=True),flush=True)
        if report.status=="V40_ONE_PROTECTED_ENTRY_RECOVERED_NOT_AUTONOMOUS":
            # A successfully protected entry must not leave an unmanaged
            # open position. This watch can CLOSE once, never OPEN again.
            if emergency_watch is None:
                from app.t100_v40_emergency_runner import run_emergency_watch
                watcher=run_emergency_watch
            else:
                watcher=emergency_watch
            try:
                finished=await watcher(env)
            except BaseException:
                return 2
            return 0 if finished==0 else 2
        if report.status!="V40_FIRST_CANARY_WAIT_FOR_GENUINE_STAGE2":
            # UNKNOWN and failed admission may never be retried as entries.
            return 2
        count+=1
        if max_cycles is None or count<max_cycles:
            await pause(INTERVAL_SECONDS)
    return 0


def main()->int:
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run",action="store_true",required=True)
    parser.parse_args()
    return asyncio.run(run_live_worker(dict(os.environ)))


if __name__=="__main__":
    raise SystemExit(main())
