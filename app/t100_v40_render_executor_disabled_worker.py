"""Default networkless HOLD, or explicit GET-only REAL V40 signal supervisor.

The existing Render worker remains hard paper/kill-latched. With NO extra
flag, it performs absolutely zero network requests, unchanged. With
T100_V40_REAL_SIGNAL_MONITOR_ENABLED=true AND
T100_V40_SINGLE_POSITION_PILOT=true it delegates ONLY to a real
V40 scanner->signed GET observer (NO order/stop/DB mutation methods).
Even if those flags are set, no live mutation authorization is possible.

Never replace this start command with an order/stop transport until an
independently reviewed safe live execution, protection and recovery path
is ready. The monitoring option is not permission to trade.
"""
from __future__ import annotations

import argparse
import json
import os
import signal
import threading
from typing import Mapping


def require_disabled_worker(env: Mapping[str,str]) -> bool:
    return (
        env.get("T100_V40_EXECUTOR_DISABLED_WORKER") == "true"
        and env.get("T100_EXECUTION_MODE") == "paper"
        and env.get("T100_LIVE_ORDER_API_ENABLED") == "false"
        and env.get("T100_LIVE_KILL_SWITCH") == "true"
        and env.get("EXECUTION_ENABLED") == "false"
        and env.get("TRADING_MODE") == "paper"
        and not env.get("DATABASE_URL")
        and not env.get("T100_V40_TRADER_API_KEY")
        and not env.get("T100_V40_TRADER_API_SECRET")
    )


def main() -> int:
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument("--run",action="store_true",required=True)
    p.parse_args()
    # A separately reviewed future first-canary release may opt into the
    # production first-signal dependency graph. Never inherit paper guards
    # or silently transform GET-only supervisor flags into live mode.
    if os.environ.get("T100_V40_FIRST_CANARY_ORDER_WORKER_ENABLED")=="true":
        from app.t100_v40_render_real_canary_worker import run_live_worker
        import asyncio
        return asyncio.run(run_live_worker(dict(os.environ)))
    # Emergency CLOSE-ONLY mode may be deployed independently of the
    # signal-entry worker, but never runs when the usual paper flag is set.
    if os.environ.get("T100_V40_EMERGENCY_EXIT_ENABLED")=="true":
        from app.t100_v40_emergency_runner import run_emergency_watch
        import asyncio
        return asyncio.run(run_emergency_watch(dict(os.environ)))
    if not require_disabled_worker(os.environ):
        print(json.dumps({
            "status":"V40_DISABLED_WORKER_PAPER_OR_ISOLATION_HALT",
            "live_authorization":False,"entry_allowed":False,
            "mexc_posts":0,"database_writes":0,
        },sort_keys=True))
        return 2
    # Explicit first-stage live-data activation. The existing startCommand
    # remains unchanged, but this branch reads genuine Stage2 signals and
    # sends only signed GETs while the paper kill switch stays engaged.
    # It must never be treated as the actual live-order adapter.
    if os.environ.get("T100_V40_REAL_SIGNAL_MONITOR_ENABLED")=="true":
        from app.t100_v40_render_ws_supervisor import (
            run_with_optional_ws_preflight,
        )
        import asyncio
        return asyncio.run(run_with_optional_ws_preflight(dict(os.environ)))
    print(json.dumps({
        "status":"V40_DISABLED_EXECUTOR_HOLDING_PROCESS",
        "entry_allowed":False,"live_authorization":False,
        "mexc_posts":0,"database_writes":0,"network_requests":0,
    },sort_keys=True),flush=True)
    done=threading.Event()
    try:
        signal.signal(signal.SIGINT,lambda _s,_f:done.set())
        signal.signal(signal.SIGTERM,lambda _s,_f:done.set())
    except ValueError:
        return 2
    while not done.wait(3600):
        print("V40_EXECUTOR_STILL_DISABLED no_orders=true",flush=True)
    return 0


if __name__=="__main__":
    raise SystemExit(main())
