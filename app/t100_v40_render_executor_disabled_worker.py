"""Safe, networkless HOLDING process for repurposed old Render paper trader.

This is deliberately NOT a live execution adapter. The trader service can
build and run the new V40 branch *before* private MEXC credentials are added.
It does not import an exchange, database, notifier, or signing client.

Use this as the repurposed Render service's startCommand while bootstrapping
and running short-lived one-off GET-only jobs. Never replace it with an
order path until reviewed + certified live release.
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
        and not env.get("MEXC_API_KEY")
        and not env.get("MEXC_API_SECRET")
    )


def main() -> int:
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument("--run",action="store_true",required=True)
    p.parse_args()
    if not require_disabled_worker(os.environ):
        print(json.dumps({
            "status":"V40_DISABLED_WORKER_PAPER_OR_ISOLATION_HALT",
            "live_authorization":False,"entry_allowed":False,
            "mexc_posts":0,"database_writes":0,
        },sort_keys=True))
        return 2
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
