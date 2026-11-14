"""Non-authorizing one-shot companion probes beside the V40 signal monitor.

Both the private WebSocket contract and current API-key futures write scope
can be checked with READ-ONLY network operations. They run independently of
the primary Stage2/ledger/GET monitor and never enable or submit orders.
"""
from __future__ import annotations

import asyncio
from dataclasses import asdict
import json
from typing import Awaitable, Callable, Mapping

from app.t100_v40_render_supervised_pilot_worker import (
    run_supervised_signal_monitor,
)
from app.t100_v40_render_private_ws_get_only import (
    FLAG, V40WsReadOnlyPreflight, observe_v40_private_ws_get_only,
)
from app.t100_v40_canary_key_permissions import (
    _FLAG as KEY_PERMISSION_FLAG,
    V40KeyPermissionObservation, report_from_env,
)
from app.t100_v40_canary_daily_loss import (
    FLAG as DAILY_LOSS_FLAG,
    V40DailyLossEvidence, observe_render_daily_loss,
)


async def run_with_optional_ws_preflight(
    env:Mapping[str,str],
    *,
    monitor:Callable[...,Awaitable[int]]=run_supervised_signal_monitor,
    ws_observe:Callable[...,Awaitable[V40WsReadOnlyPreflight]]=
        observe_v40_private_ws_get_only,
    key_observe:Callable[...,Awaitable[V40KeyPermissionObservation]]=
        report_from_env,
    daily_observe:Callable[...,Awaitable[V40DailyLossEvidence]]=
        observe_render_daily_loss,
    emit:Callable[[str],object]=print,
) -> int:
    """Start primary monitor immediately; neither diagnostic can block it."""
    do_ws=env.get(FLAG)=="true"
    do_key=env.get(KEY_PERMISSION_FLAG)=="true"
    do_daily=env.get(DAILY_LOSS_FLAG)=="true"
    if not do_ws and not do_key and not do_daily:
        return await monitor(env)

    async def bounded_ws_diagnostic() -> None:
        try:
            report=await asyncio.wait_for(ws_observe(env),timeout=45)
            if type(report) is not V40WsReadOnlyPreflight:
                raise ValueError("WS_PREFLIGHT_REPORT_UNVERIFIED")
            emit(json.dumps(asdict(report),sort_keys=True))
        except asyncio.CancelledError:
            raise
        except Exception:
            emit(json.dumps(asdict(V40WsReadOnlyPreflight(
                "V40_WS_PREFLIGHT_UNVERIFIED_OR_TIMED_OUT_HALT",
            )),sort_keys=True))

    async def bounded_key_diagnostic() -> None:
        try:
            report=await asyncio.wait_for(key_observe(env),timeout=12)
            if type(report) is not V40KeyPermissionObservation:
                raise ValueError("KEY_PERMISSION_REPORT_UNVERIFIED")
            emit(json.dumps(asdict(report),sort_keys=True))
        except asyncio.CancelledError:
            raise
        except Exception:
            emit(json.dumps(asdict(V40KeyPermissionObservation(
                "V40_KEY_INFO_UNEXPECTED_HALT",
            )),sort_keys=True))

    async def bounded_daily_loss_diagnostic() -> None:
        try:
            report=await asyncio.wait_for(daily_observe(env),timeout=33)
            if type(report) is not V40DailyLossEvidence:
                raise ValueError("V40_DAILY_LOSS_REPORT_UNVERIFIED")
            emit(json.dumps(asdict(report),sort_keys=True))
        except asyncio.CancelledError:
            raise
        except Exception:
            emit(json.dumps(asdict(V40DailyLossEvidence(
                "V40_DAILY_LOSS_UNEXPECTED_OR_TIMED_OUT_HALT",
            )),sort_keys=True))

    tasks=[]
    if do_ws:
        tasks.append(asyncio.create_task(bounded_ws_diagnostic()))
    if do_key:
        tasks.append(asyncio.create_task(bounded_key_diagnostic()))
    if do_daily:
        tasks.append(asyncio.create_task(bounded_daily_loss_diagnostic()))
    try:
        return await monitor(env)
    finally:
        for task in tasks:
            if not task.done():
                task.cancel()
        await asyncio.gather(*tasks,return_exceptions=True)
