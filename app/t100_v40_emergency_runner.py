"""Supervised V40 emergency close/GET recovery for dedicated first pilot.

A separate, opt-in CLOSE-ONLY runtime. It cannot open shorts or perform
general portfolio trading. On every invocation:
  1. Read immutable prior emergency claims FIRST; if found, GET-reconcile ONLY.
  2. Else prove an exact checkpoint-owned unprotected SHORT by two signed
     full-account+symbol GET bookends and a complete immutable SQL history.
  3. Insert ONE durable claim; immediately repeat independent signed GET.
  4. Send at most ONE side=2 market CLOSE SHORT with exact positionId.
  5. Independently reconcile order/deals and two flat signed GETs, then
     persist CLOSE_SHORT fills, CLOSED checkpoint/intent and release slot.
Any uncertain result never retries exchange POST. No paper/mock signals.
"""
from __future__ import annotations

import asyncio
from dataclasses import asdict,dataclass
import os
from typing import Any,Mapping,Awaitable,Callable

from app.t100_v40_emergency_close import (
    V40EmergencyCloseTarget,V40EmergencyCloseAdapter,
    V40EmergencyCloseHalted,close_once_after_durable_claim,
)
from app.t100_v40_emergency_close_claims import V40PostgresEmergencyCloseClaims
from app.t100_v40_emergency_observer import (
    observe_emergency_target,_signed_frame,
)
from app.t100_v40_emergency_postflight import assess_emergency_close_postflight
from app.t100_v40_emergency_completion import PostgresV40EmergencyCompletion
from app.t100_live_readonly_probe import ReadOnlyMexcProbe

FLAG="T100_V40_EMERGENCY_EXIT_ENABLED"


@dataclass(frozen=True)
class V40EmergencyRunnerStatus:
    status:str
    claim_already_existed:bool=False
    claim_committed:bool=False
    exchange_close_post_may_have_occurred:bool=False
    signed_close_and_flat_verified:bool=False
    durable_checkpoint_and_slot_closed:bool=False
    further_close_orders_allowed:bool=False
    new_entries_allowed:bool=False
    live_authorization:bool=False


def emergency_environment(env:Mapping[str,str])->bool:
    # Emergency exit is separate from new entry and may operate when guardian
    # is latched, but ONLY on the exact owned V40 position with a DB claim.
    return all((
        env.get(FLAG)=="true",
        env.get("T100_V40_ACCOUNT_PROFILE")=="DEDICATED_WEB_PILOT",
        env.get("T100_V40_SINGLE_POSITION_PILOT")=="true",
        env.get("T100_EXECUTION_MODE")=="live",
        env.get("T100_LIVE_ORDER_API_ENABLED")=="true",
        env.get("T100_LIVE_KILL_SWITCH")=="false",
        env.get("TRADING_MODE")=="live",
        env.get("EXECUTION_ENABLED")=="true",
        env.get("T100_V40_EXECUTOR_DISABLED_WORKER")=="false",
        not env.get("DATABASE_URL"),
        not env.get("T100_V40_TRADER_API_KEY"),
        not env.get("T100_V40_TRADER_API_SECRET"),
    ))


async def _reconcile_only(
    *,target:V40EmergencyCloseTarget,
    probe:ReadOnlyMexcProbe,
    pool:Any,
    existing_claim:bool,
    delay:Callable[[float],Awaitable[Any]]=asyncio.sleep,
)->V40EmergencyRunnerStatus:
    """Never submits orders, including on missing or delayed exchange ACK."""
    base=dict(
        claim_already_existed=existing_claim,
        claim_committed=True,
        exchange_close_post_may_have_occurred=True,
    )
    try:
        close=await probe.raw_order_by_external_oid(
            target.symbol,target.close_external_oid,
        )
        if close is None:
            return V40EmergencyRunnerStatus(
                "V40_EMERGENCY_CLOSE_UNKNOWN_POST_NO_ORDER_RETRY",**base,
            )
        order_id=str(close.get("orderId") or "")
        if not order_id.isascii() or not order_id.isdigit() or not int(order_id)>0:
            raise ValueError
        deals=await probe.raw_order_deal_details(order_id)
        first=await _signed_frame(probe,target.symbol)
        await delay(.3)
        second=await _signed_frame(probe,target.symbol)
        proof=assess_emergency_close_postflight(
            target=target,close_order=close,deals=deals,
            flat_first=first,flat_second=second,
        )
        if proof.status!=(
            "V40_EMERGENCY_CLOSE_ORDER_AND_FLAT_ACCOUNT_DOUBLE_GET_VERIFIED"
        ):
            return V40EmergencyRunnerStatus(
                "V40_EMERGENCY_CLOSE_POST_ACK_AWAIT_SIGNED_FLAT_RECONCILIATION",
                **base,
            )
        complete=await PostgresV40EmergencyCompletion(pool).finalize_verified(
            target=target,postflight=proof,
        )
        if complete.status!=(
            "V40_EMERGENCY_CLOSE_CHECKPOINT_INTENT_AND_SLOT_CLOSED_VERIFIED"
        ):
            raise ValueError
        return V40EmergencyRunnerStatus(
            complete.status,**base,
            signed_close_and_flat_verified=True,
            durable_checkpoint_and_slot_closed=True,
        )
    except Exception:
        return V40EmergencyRunnerStatus(
            "V40_EMERGENCY_CLOSE_GET_OR_DB_RECONCILIATION_UNVERIFIED",
            **base,
        )


async def run_v40_emergency_close_once(
    env:Mapping[str,str],*,
    pool:Any,
    probe:ReadOnlyMexcProbe,
    public:Any,
    venue:Any,
)->V40EmergencyRunnerStatus:
    """Dependency-injectable one-shot, MEXC client is real only in network wrapper."""
    if not emergency_environment(env):
        return V40EmergencyRunnerStatus(
            "V40_EMERGENCY_EXIT_ACCOUNT_OR_LIVE_CONTROLS_REQUIRED"
        )
    if (
        pool is None or type(probe) is not ReadOnlyMexcProbe
        or venue is None
        or type(getattr(venue,"api_key",None)) is not str
        or venue.api_key!=getattr(probe._client,"_key",None)
        or venue.api_secret!=getattr(probe._client,"_secret",None)
        or not callable(getattr(venue,"_private_post",None))
    ):
        return V40EmergencyRunnerStatus(
            "V40_EMERGENCY_EXIT_SIGNED_ACCOUNT_IDENTITY_INVALID"
        )
    claims=V40PostgresEmergencyCloseClaims(pool)
    try:
        prior=await claims.read_prior_claim()
    except Exception:
        return V40EmergencyRunnerStatus(
            "V40_EMERGENCY_EXIT_DB_CLAIM_READER_FAILED_NO_POST"
        )
    if prior is not None:
        return await _reconcile_only(
            target=prior,probe=probe,pool=pool,existing_claim=True,
        )
    initial=await observe_emergency_target(
        pool=pool,probe=probe,public=public,
    )
    if initial.status!=(
        "V40_EMERGENCY_OWNED_SHORT_MISSING_NATIVE_SL_TWO_GETS"
    ) or type(initial.target) is not V40EmergencyCloseTarget:
        return V40EmergencyRunnerStatus(
            "V40_EMERGENCY_EXIT_NO_CERTIFIED_MISSING_STOP_NO_POST",
        )
    target=initial.target
    async def fresh():
        observed=await observe_emergency_target(
            pool=pool,probe=probe,public=public,
        )
        if type(observed.target) is not V40EmergencyCloseTarget:
            raise V40EmergencyCloseHalted(
                "V40_EMERGENCY_EXIT_PREPOST_SIGNED_GET_FAILED"
            )
        return observed.target
    try:
        await close_once_after_durable_claim(
            target=target,claimant=claims,
            adapter=V40EmergencyCloseAdapter(venue),
            pre_post_recheck=fresh,
        )
    except Exception:
        # Missing claim ACK is ambiguous: some SQL may have committed.
        # Never resubmit; on next run, query immutable prior claim.
        return V40EmergencyRunnerStatus(
            "V40_EMERGENCY_EXIT_CLAIM_OR_SIGNED_PREPOST_UNVERIFIED_NO_RETRY"
        )
    return await _reconcile_only(
        target=target,probe=probe,pool=pool,existing_claim=False,
    )


async def with_render_network(
    env:Mapping[str,str],
)->V40EmergencyRunnerStatus:
    """Bind the same dedicated pilot signed GET and MEXC POST key."""
    if not emergency_environment(env):
        return V40EmergencyRunnerStatus(
            "V40_EMERGENCY_EXIT_ACCOUNT_OR_LIVE_CONTROLS_REQUIRED"
        )
    dsn=env.get("T100_V40_EXECUTOR_RUNTIME_DSN","")
    key=env.get("MEXC_API_KEY","")
    secret=env.get("MEXC_API_SECRET","")
    if (
        type(dsn) is not str or not dsn.startswith(("postgresql://","postgres://"))
        or type(key) is not str or len(key)<12
        or type(secret) is not str or len(secret)<16
    ):
        return V40EmergencyRunnerStatus(
            "V40_EMERGENCY_EXIT_RUNTIME_DSN_OR_PILOT_KEY_INVALID"
        )
    import asyncpg
    import httpx
    from app.mexc_trade import MexcTradeClient
    from app.t100_v40_executor_assembly import V40FirstPilotGetOnly
    pool=None
    try:
        pool=await asyncpg.create_pool(
            dsn=dsn,ssl="require",min_size=1,max_size=1,
            timeout=12,command_timeout=15,
        )
        async with httpx.AsyncClient(
            base_url="https://api.mexc.com",
            trust_env=False,follow_redirects=False,
            timeout=httpx.Timeout(8.0),
        ) as get, httpx.AsyncClient(
            base_url="https://api.mexc.com",
            trust_env=False,follow_redirects=False,
            timeout=httpx.Timeout(8.0),
        ) as public:
            venue=MexcTradeClient(
                "https://api.mexc.com",api_key=key,api_secret=secret,
            )
            try:
                return await run_v40_emergency_close_once(
                    env,pool=pool,
                    probe=ReadOnlyMexcProbe(V40FirstPilotGetOnly(
                        get,api_key=key,api_secret=secret,
                    )),
                    public=public,venue=venue,
                )
            finally:
                await venue.close()
    except Exception:
        return V40EmergencyRunnerStatus(
            "V40_EMERGENCY_EXIT_NETWORK_OR_DB_UNAVAILABLE_NO_RETRY"
        )
    finally:
        if pool is not None:
            await pool.close()
