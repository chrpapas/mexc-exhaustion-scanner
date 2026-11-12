"""One-cycle T100 V40 private WS + signed REST correlation, GET-only.

Runs against the dedicated V40 pilot account only under the existing
read-only, kill-latched Render monitor controls. Never submits, cancels, or
modifies orders and cannot issue or accept a trading permit. An observed
login/subscription on a FLAT account is NOT certification of stop-trigger,
fill, account completeness or autonomous trading protection.

The runtime reuses the exact allowlisted guardian GET transport, never the
MEXC POST-capable trader client. Only the official pinned MEXC Futures WS
and REST endpoints may receive the credentials. No raw events/secrets/URLs
are logged. This opt-in diagnostic is NOT an independent-host guardian.
"""
from __future__ import annotations

import argparse
import asyncio
from dataclasses import dataclass, asdict
import json
import os
from typing import Any, Awaitable, Callable, Mapping

from app.t100_v40_render_supervised_pilot_worker import monitor_environment
from app.t100_live_guardian_readonly_once import GetOnlyMexcTransport
from app.t100_live_ws_rest_correlation import run_ws_rest_lifecycle_probe

FLAG="T100_V40_PRIVATE_WS_GET_ONLY_PREFLIGHT_ENABLED"
DURATION_SECONDS=20.0


@dataclass(frozen=True)
class V40WsReadOnlyPreflight:
    status: str
    ws_login_observed: bool=False
    ws_filter_observed: bool=False
    ws_semantic_events: int=0
    ws_rest_position_correlations: int=0
    ws_rest_stop_correlations: int=0
    ws_rest_fill_correlations: int=0
    ws_rest_failures: int=0
    full_lifecycle_certified: bool=False
    independent_guardian_connected: bool=False
    live_adapter_connected: bool=False
    native_stop_execution_certified: bool=False
    account_wide_completeness_certified: bool=False
    exchange_posts: int=0
    database_writes: int=0
    read_only: bool=True
    entry_allowed: bool=False
    live_authorization: bool=False


def configured(env: Mapping[str,str]) -> bool:
    return monitor_environment(env) and env.get(FLAG)=="true"


def assess_ws_rest_preflight(response: Any) -> V40WsReadOnlyPreflight:
    """Interpret only strict, redacted already-read WS/REST results."""
    def failed(status:str) -> V40WsReadOnlyPreflight:
        return V40WsReadOnlyPreflight(status=status)
    if (
        type(response) is not dict
        or response.get("read_only") is not True
        or response.get("live_order_submission_possible") is not False
        or response.get("entry_allowed") is not False
        or response.get("live_authorization") is not False
    ):
        return failed("V40_WS_REST_READONLY_RESULT_INVALID_HALT")
    ws=response.get("ws_observation")
    corr=response.get("ws_rest_correlation")
    if type(ws) is not dict or type(corr) is not dict:
        return failed("V40_WS_REST_EVIDENCE_COLLECTION_INVALID_HALT")
    count_keys=(
        "position_events_correlated","stop_events_correlated",
        "fill_events_correlated","position_correlation_failures",
        "stop_correlation_failures","fill_correlation_failures",
    )
    if any(type(corr.get(key)) is not int or corr[key]<0 for key in count_keys):
        return failed("V40_WS_REST_COUNTS_MALFORMED_HALT")
    count_ws=("private_events_seen","payload_semantic_events_verified",
              "payload_semantic_failures")
    if any(type(ws.get(key)) is not int or ws[key]<0 for key in count_ws):
        return failed("V40_WS_REST_WS_COUNTS_MALFORMED_HALT")
    if not (
        ws.get("read_only") is True
        and ws.get("entry_allowed") is False
        and ws.get("live_authorization") is False
        and ws.get("login_verified") is True
        and ws.get("filter_sent") is True
        and ws.get("status")=="COMPLETED_PRIVATE_WS_OBSERVATION"
        and ws.get("unexpected_private_channels") in ([],())
        and ws["private_events_seen"]==ws["payload_semantic_events_verified"]
        and ws["payload_semantic_failures"]==0
        and corr.get("live_authorization") is False
    ):
        return failed("V40_WS_REST_PRIVATE_LOGIN_OR_SEMANTICS_HALT")
    failures=sum(corr[k] for k in (
        "position_correlation_failures","stop_correlation_failures",
        "fill_correlation_failures",
    ))
    return V40WsReadOnlyPreflight(
        status=(
            "V40_WS_REST_LIFECYCLE_CORRELATED_OBSERVED_NONAUTHORIZING"
            if corr.get("private_ws_full_lifecycle_verified") is True
            and failures==0 else
            "V40_WS_LOGIN_FILTER_OBSERVED_LIFECYCLE_NOT_CERTIFIED"
        ),
        ws_login_observed=True,ws_filter_observed=True,
        ws_semantic_events=ws["payload_semantic_events_verified"],
        ws_rest_position_correlations=corr["position_events_correlated"],
        ws_rest_stop_correlations=corr["stop_events_correlated"],
        ws_rest_fill_correlations=corr["fill_events_correlated"],
        ws_rest_failures=failures,
        # Even full WS/REST current-row correlation is not sufficient
        # to certify native market-stop execution, position completeness,
        # or an independent durable nonflat risk guardian.
    )


async def observe_v40_private_ws_get_only(
    env:Mapping[str,str],
    *,
    observe:Callable[...,Awaitable[dict[str,Any]]]=run_ws_rest_lifecycle_probe,
) -> V40WsReadOnlyPreflight:
    if not configured(env):
        return V40WsReadOnlyPreflight(
            "V40_WS_PAPER_KILL_OR_READONLY_SCOPE_REQUIRED_HALT"
        )
    key,secret=env.get("MEXC_API_KEY",""),env.get("MEXC_API_SECRET","")
    if (
        type(key) is not str or len(key)<12 or key!=key.strip()
        or type(secret) is not str or len(secret)<16 or secret!=secret.strip()
    ):
        return V40WsReadOnlyPreflight("V40_WS_ACCOUNT_KEYS_MISSING_HALT")
    import httpx
    try:
        async with httpx.AsyncClient(
            base_url=GetOnlyMexcTransport.BASE_URL,
            trust_env=False,follow_redirects=False,
            timeout=httpx.Timeout(8.0),
        ) as http:
            get_only=GetOnlyMexcTransport(
                http,api_key=key,api_secret=secret,
            )
            # The WS implementation pins its official WSS URL by default.
            result=await asyncio.wait_for(
                observe(
                    client=get_only,api_key=key,api_secret=secret,
                    duration_seconds=DURATION_SECONDS,
                ),timeout=DURATION_SECONDS+18,
            )
    except Exception:
        return V40WsReadOnlyPreflight(
            "V40_WS_SIGNED_GET_OR_PRIVATE_WS_OBSERVATION_FAILED_HALT"
        )
    return assess_ws_rest_preflight(result)


def main() -> int:
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--once",action="store_true",required=True)
    parser.parse_args()
    try:
        result=asyncio.run(observe_v40_private_ws_get_only(dict(os.environ)))
    except Exception:
        result=V40WsReadOnlyPreflight(
            "V40_WS_PREFLIGHT_UNEXPECTED_ERROR_HALT",
        )
    print(json.dumps(asdict(result),sort_keys=True),flush=True)
    return 0 if result.ws_login_observed else 2


if __name__=="__main__":
    raise SystemExit(main())
