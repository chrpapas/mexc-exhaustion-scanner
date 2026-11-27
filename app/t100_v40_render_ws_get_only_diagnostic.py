"""Opt-in, one-shot V40 private WS/REST read-only diagnostic.

This is NOT the executor or an autonomous position guardian. Use an isolated,
dedicated MEXC monitoring key, not the BOXWAVE account. The entire REST
transport has only a restricted signed GET method, never a POST. Private WS
authenticates/subscribes to positions, native TP/SL and fills; it never sends
trade commands. Correlation of naturally occurring events is sampled, never
a proof of zero missed messages, exchange stop execution, or live readiness.

Default import and CLI (--help only) produce no network requests. An explicit
--once plus paper/kill-latched environment is required for real I/O.
Outputs deliberately omit all secrets, symbols, order IDs and raw payloads.
"""
from __future__ import annotations

import argparse
import asyncio
from dataclasses import asdict, dataclass
import json
import os
from typing import Any, Awaitable, Callable, Mapping

from app.t100_live_guardian_readonly_once import GetOnlyMexcTransport
from app.t100_live_ws_rest_correlation import run_ws_rest_lifecycle_probe


@dataclass(frozen=True)
class V40WsGetOnlyReport:
    status: str
    login_filter_verified: bool = False
    full_rest_correlated_lifecycle: bool = False
    observed_private_events: int = 0
    correlated_position_events: int = 0
    correlated_stop_events: int = 0
    correlated_fill_events: int = 0
    rest_correlation_failures: int = 0
    read_only: bool = True
    exchange_permission_certified: bool = False
    exchange_stop_execution_certified: bool = False
    live_authorization: bool = False
    entry_allowed: bool = False
    mexc_posts: int = 0
    database_writes: int = 0


def require_ws_readonly_scope(env: Mapping[str,str],duration_seconds:int) -> None:
    """Configuration is a *negative* permission boundary, never live auth."""
    if not (
        type(duration_seconds) is int and 15<=duration_seconds<=90
        and env.get("T100_V40_WS_GET_ONLY_PROBE_ENABLED")=="true"
        and env.get("T100_V40_EXECUTOR_DISABLED_WORKER")=="true"
        and env.get("T100_V40_REAL_SIGNAL_MONITOR_ENABLED")=="true"
        and env.get("T100_V40_MONITOR_SINGLE_POSITION_SCOPE")=="true"
        and env.get("T100_EXECUTION_MODE")=="paper"
        and env.get("T100_LIVE_ORDER_API_ENABLED")=="false"
        and env.get("T100_LIVE_KILL_SWITCH")=="true"
        and env.get("EXECUTION_ENABLED")=="false"
        and env.get("TRADING_MODE")=="paper"
        and not env.get("DATABASE_URL")
        and not env.get("T100_V40_TRADER_API_KEY")
        and not env.get("T100_V40_TRADER_API_SECRET")
    ):
        raise ValueError("V40_WS_DIAGNOSTIC_PAPER_KILL_SCOPE_REQUIRED")
    key,secret=env.get("MEXC_API_KEY"),env.get("MEXC_API_SECRET")
    if (
        type(key) is not str or len(key)<12 or key!=key.strip()
        or type(secret) is not str or len(secret)<16 or secret!=secret.strip()
    ):
        raise ValueError("V40_WS_DIAGNOSTIC_ACCOUNT_CREDENTIALS_UNVERIFIED")


def _nonnegative(report:Mapping[str,Any],key:str)->int:
    value=report.get(key)
    if type(value) is not int or value<0:
        raise ValueError("V40_WS_DIAGNOSTIC_CORRELATION_SHAPE_INVALID")
    return value


async def observe_ws_once(
    env:Mapping[str,str], *,
    duration_seconds:int=30,
    lifecycle_probe:Callable[...,Awaitable[dict[str,Any]]]=run_ws_rest_lifecycle_probe,
    http_client:Any|None=None,
    connect:Any|None=None,
)->V40WsGetOnlyReport:
    """One bounded WS observer plus signed GET correlation; no DB/POST."""
    try:
        require_ws_readonly_scope(env,duration_seconds)
    except Exception:
        return V40WsGetOnlyReport("V40_WS_DIAGNOSTIC_PAPER_KILL_OR_SCOPE_HALT")
    key,secret=env["MEXC_API_KEY"],env["MEXC_API_SECRET"]

    async def inspect(client:Any) -> V40WsGetOnlyReport:
        transport=GetOnlyMexcTransport(
            client,api_key=key,api_secret=secret,
        )
        # Defensive: a developer accidentally adding a write method to this
        # transport is a hard boundary failure, not permission to trade.
        if hasattr(transport,"_private_post"):
            return V40WsGetOnlyReport("V40_WS_DIAGNOSTIC_GET_ONLY_TRANSPORT_CHANGED")
        try:
            result=await asyncio.wait_for(
                lifecycle_probe(
                    client=transport,api_key=key,api_secret=secret,
                    duration_seconds=duration_seconds,connect=connect,
                ),
                timeout=duration_seconds+20,
            )
            if (
                type(result) is not dict
                or result.get("read_only") is not True
                or result.get("live_order_submission_possible") is not False
                or result.get("entry_allowed") is not False
                or result.get("live_authorization") is not False
                or type(result.get("ws_observation")) is not dict
                or type(result.get("ws_rest_correlation")) is not dict
            ):
                return V40WsGetOnlyReport("V40_WS_DIAGNOSTIC_RESULT_SHAPE_UNVERIFIED")
            ws=result["ws_observation"]
            rest=result["ws_rest_correlation"]
            if not (
                ws.get("read_only") is True
                and ws.get("entry_allowed") is False
                and ws.get("live_authorization") is False
                and ws.get("login_verified") is True
                and ws.get("filter_sent") is True
                and ws.get("unexpected_private_channels") in ((),[])
                and ws.get("status")=="COMPLETED_PRIVATE_WS_OBSERVATION"
                and rest.get("live_authorization") is False
                and result.get("private_ws_pre_pilot_contract_verified") is True
            ):
                return V40WsGetOnlyReport("V40_WS_DIAGNOSTIC_LOGIN_FILTER_OR_CONTRACT_UNVERIFIED")
            count=_nonnegative(ws,"private_events_seen")
            correlated_position=_nonnegative(rest,"position_events_correlated")
            correlated_stop=_nonnegative(rest,"stop_events_correlated")
            correlated_fill=_nonnegative(rest,"fill_events_correlated")
            failures=sum(
                _nonnegative(rest,key)
                for key in (
                    "position_correlation_failures",
                    "stop_correlation_failures",
                    "fill_correlation_failures",
                )
            )
            if count!=sum(_nonnegative(rest,key) for key in (
                "position_events_observed","stop_events_observed",
                "fill_events_observed",
            )):
                return V40WsGetOnlyReport("V40_WS_DIAGNOSTIC_EVENT_COUNTS_DRIFT")
            full=(
                result.get("private_ws_full_lifecycle_verified") is True
                and rest.get("private_ws_full_lifecycle_verified") is True
                and all(x>0 for x in (
                    correlated_position,correlated_stop,correlated_fill,
                ))
                and failures==0
            )
            return V40WsGetOnlyReport(
                status=(
                    "V40_WS_REST_LIFECYCLE_CORRELATED_GET_ONLY_NONAUTHORIZING"
                    if full else
                    "V40_WS_LOGIN_FILTER_VERIFIED_LIFECYCLE_INCOMPLETE_GET_ONLY"
                ),
                login_filter_verified=True,
                full_rest_correlated_lifecycle=full,
                observed_private_events=count,
                correlated_position_events=correlated_position,
                correlated_stop_events=correlated_stop,
                correlated_fill_events=correlated_fill,
                rest_correlation_failures=failures,
            )
        except BaseException:
            # An exception could embed live HTTP headers/credentials. Never
            # stringify the exception or include raw venue payloads in logs.
            return V40WsGetOnlyReport("V40_WS_DIAGNOSTIC_NETWORK_OR_CORRELATION_HALT")

    if http_client is not None:
        return await inspect(http_client)
    try:
        import httpx
        async with httpx.AsyncClient(
            base_url=GetOnlyMexcTransport.BASE_URL,
            follow_redirects=False,trust_env=False,
            timeout=httpx.Timeout(8.0),
        ) as client:
            return await inspect(client)
    except BaseException:
        return V40WsGetOnlyReport("V40_WS_DIAGNOSTIC_NETWORK_OR_CORRELATION_HALT")


def main() -> int:
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--once",action="store_true")
    parser.add_argument("--seconds",type=int,default=30)
    args=parser.parse_args()
    if not args.once:
        print(json.dumps(asdict(V40WsGetOnlyReport(
            "V40_WS_DIAGNOSTIC_NOT_STARTED_REQUIRES_EXPLICIT_ONCE"
        )),sort_keys=True))
        return 2
    result=asyncio.run(observe_ws_once(
        dict(os.environ),duration_seconds=args.seconds,
    ))
    print(json.dumps(asdict(result),sort_keys=True),flush=True)
    return 0 if result.login_filter_verified else 2


if __name__=="__main__":
    raise SystemExit(main())
