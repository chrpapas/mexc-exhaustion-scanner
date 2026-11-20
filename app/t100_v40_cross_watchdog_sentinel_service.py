"""Second local process: monitor V40 watchdog journal, NOT MEXC API.

Shares no MEXC credentials and no order transport with the primary watcher.
Needs a separate Discord webhook stored in its own Mac Keychain service.
This is a local-process death alarm, NOT independent-host or exchange-loss
protection; no services are installed automatically by this module.
"""
from __future__ import annotations

import argparse
import asyncio
import os
from pathlib import Path
import re
import sys
from typing import Mapping

from app.t100_live_ops_notifier import OpsNotice,T100LiveOpsNotifier
from app.t100_pilot_watchdog_sentinel import PilotLivenessSentinel
from scripts.t100_v40_pilot_get_only import _from_keychain
from app.t100_v40_cross_watchdog_service import (
    DISCORD_ACCOUNT,KEYCHAIN_DISCORD,
)

SENTINEL_KEYCHAIN="t100225-v40-watchdog-sentinel-discord-v1"


class V40SentinelHalted(RuntimeError):
    pass


def config(env:Mapping[str,str],*,cycles:int|None):
    if not all((
        env.get("T100_EXECUTION_MODE")=="paper",
        env.get("T100_LIVE_ORDER_API_ENABLED")=="false",
        env.get("T100_LIVE_KILL_SWITCH")=="true",
        env.get("T100_V40_SENTINEL_ENABLED")=="true",
        env.get("T100_V40_SENTINEL_DISCORD_KEYCHAIN_SERVICE")==SENTINEL_KEYCHAIN,
    )):
        raise V40SentinelHalted("WATCH_SENTINEL_PAPER_KILL_REQUIRED")
    sha=env.get("T100_V40_SENTINEL_SCOPE_SHA256","")
    if re.fullmatch(r"[a-f0-9]{64}",sha) is None:
        raise V40SentinelHalted("WATCH_SENTINEL_EXACT_SCOPE_REQUIRED")
    watcher=Path(env.get("T100_V40_SENTINEL_WATCHDOG_JOURNAL",""))
    outbox=Path(env.get("T100_V40_SENTINEL_OUTBOX_JOURNAL",""))
    if not (
        watcher.is_absolute() and outbox.is_absolute()
        and watcher!=outbox and not watcher.is_symlink()
        and not outbox.is_symlink()
    ):
        raise V40SentinelHalted("WATCH_SENTINEL_SEPARATE_JOURNALS_REQUIRED")
    if cycles is not None and (type(cycles) is not int or cycles<1):
        raise V40SentinelHalted("WATCH_SENTINEL_CYCLES_INVALID")
    return watcher,outbox,sha


async def run_sentinel(
    *,environ:Mapping[str,str]|None=None,cycles:int|None=1,
):
    env=os.environ if environ is None else environ
    watched,outbox,sha=config(env,cycles=cycles)
    primary=_from_keychain(KEYCHAIN_DISCORD,DISCORD_ACCOUNT)
    secondary=_from_keychain(SENTINEL_KEYCHAIN,DISCORD_ACCOUNT)
    pattern=r"https://discord\.com/api/webhooks/[0-9]+/[A-Za-z0-9_-]+"
    if (
        re.fullmatch(pattern,primary) is None
        or re.fullmatch(pattern,secondary) is None
        or primary==secondary
    ):
        raise V40SentinelHalted("WATCH_SENTINEL_INDEPENDENT_DISCORD_REQUIRED")
    notifier=T100LiveOpsNotifier(secondary,cooldown_seconds=0)
    async def notify(code):
        if re.fullmatch(r"WATCH_(HEARTBEAT|SENTINEL)_[A-Z0-9_]{1,90}",code) is None:
            return False
        return await notifier.send(OpsNotice(
            key="t100-v40-death-"+code,
            title="T100 V40 watchdog stopped or stale",
            description=code,
            severity="critical",
        ),force=True)
    try:
        with PilotLivenessSentinel(
            watched_journal=watched,
            outbox_journal=outbox,
            expected_scope_sha256=sha,
            notifier=notify,
            stale_after_ms=30000,poll_seconds=5.0,
            notify_timeout_seconds=8.0,
        ) as sentinel:
            return await sentinel.run(max_cycles=cycles)
    finally:
        await notifier.close()


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    args=parser.add_mutually_exclusive_group()
    args.add_argument("--cycles",type=int,default=1)
    args.add_argument("--continuous",action="store_true")
    flags=parser.parse_args()
    try:
        result=asyncio.run(run_sentinel(
            cycles=None if flags.continuous else flags.cycles,
        ))
        print("T100 V40 INDEPENDENT LOCAL SENTINEL:",result.status)
        print("NO MEXC API ACCESS OR ORDERS.")
        if result.alarm_latched or result.notifications_pending:
            raise SystemExit(2)
    except Exception:
        print("T100 V40 SENTINEL HALTED_FAIL_CLOSED. NO MEXC ACCESS.")
        raise SystemExit(2)


if __name__=="__main__":
    main()
