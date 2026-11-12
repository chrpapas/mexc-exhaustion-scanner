"""Non-authorizing companion one-shot WS/REST preflight beside V40 monitor.

This never converts a successful private WS login into an entry permit.
The independent continuous guardian remains NOT connected. An optional
20-second read-only WS diagnostic runs concurrently with the existing
signed GET + restricted Stage2 + journal monitor (which remains primary).
Failure is logged without suppressing the primary scanner/account heartbeat.
"""
from __future__ import annotations

import asyncio
from dataclasses import asdict
import json
from typing import Awaitable,Callable,Mapping

from app.t100_v40_render_supervised_pilot_worker import (
    run_supervised_signal_monitor,
)
from app.t100_v40_render_private_ws_get_only import (
    FLAG,V40WsReadOnlyPreflight,observe_v40_private_ws_get_only,
)


async def run_with_optional_ws_preflight(
    env:Mapping[str,str],
    *,
    monitor:Callable[...,Awaitable[int]]=run_supervised_signal_monitor,
    ws_observe:Callable[...,Awaitable[V40WsReadOnlyPreflight]]=
        observe_v40_private_ws_get_only,
    emit:Callable[[str],object]=print,
) -> int:
    """Start primary observer immediately. Optional WS may never block it."""
    if env.get(FLAG)!="true":
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

    task=asyncio.create_task(bounded_ws_diagnostic())
    try:
        return await monitor(env)
    finally:
        if not task.done():
            task.cancel()
        await asyncio.gather(task,return_exceptions=True)
