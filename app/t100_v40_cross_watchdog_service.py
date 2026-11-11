"""Standalone Mac-local V40 FLAT/CROSS-1x GET-only alarm service candidate.

Opt-in and NOT deployed. Uses dedicated READ-ONLY account key from macOS
Keychain, a separately provisioned Discord-only Keychain webhook, durable
SQLite alarm journal and explicit SHA-256 account-key pin. This file cannot
submit/cancel/amend MEXC orders. Account-key SHA pin is NOT independent
proof of the exchange subaccount identity or its server-side key permissions.

The FLAT scope alarms on any new exposure. Switching to OWNED requires an
operator-reviewed exact entry, native stop, position and fill manifest and
a separate journal scope; never automatically adopts observed orders.
"""
from __future__ import annotations

import argparse
import asyncio
import hashlib
import hmac
import json
import os
from pathlib import Path
import re
import sys
import time
from dataclasses import dataclass
from decimal import Decimal,InvalidOperation
from typing import Any,Mapping

import httpx

from app.t100_live_ops_notifier import OpsNotice,T100LiveOpsNotifier
from app.t100_live_readonly_probe import ReadOnlyMexcProbe
from app.t100_pilot_watchdog_runtime import PilotWatchdogRunner
from app.t100_v40_cross_watchdog_observer import (
    V40CrossWatchScope,observe_v40_cross_get,validate_v40_scope,
)
from scripts.t100_v40_pilot_get_only import (
    KEY_ACCOUNT,SECRET_ACCOUNT,_from_keychain,
)

MEXC_BASE="https://api.mexc.com"
KEYCHAIN_READONLY="t100225-mexc-web-pilot-readonly-v1"
KEYCHAIN_DISCORD="t100225-v40-watchdog-discord-v1"
DISCORD_ACCOUNT="DISCORD_WEBHOOK_URL"
_EXACT=frozenset({
    "/api/v1/private/position/open_positions",
    "/api/v1/private/stoporder/open_orders",
    "/api/v1/private/order/open_order_total_count",
    "/api/v1/private/position/position_mode",
})
_EXTERNAL=re.compile(
    r"/api/v1/private/order/external/[A-Z0-9]+_USDT/"
    r"t100[a-zA-Z0-9_]{8,60}"
)
_DEALS=re.compile(r"/api/v1/private/order/deal_details/[1-9][0-9]*")


class V40WatchServiceHalted(RuntimeError):
    """Fixed safe status only; never show secrets, raw GETs or paths."""


class V40WatchSignedGetOnly:
    """Only HTTP GET to explicitly allowlisted MEXC paths; no POST API."""

    def __init__(self,key:str,secret:str,*,timeout:float=4.0):
        if (
            type(key) is not str or len(key)<12
            or type(secret) is not str or len(secret)<16
            or type(timeout) not in (float,int) or not 0<timeout<=5
        ):
            raise V40WatchServiceHalted("WATCH_V40_SIGNED_GET_CONFIG_INVALID")
        self._key=key
        self._secret=secret
        self._http=httpx.AsyncClient(
            base_url=MEXC_BASE,trust_env=False,follow_redirects=False,
            timeout=httpx.Timeout(timeout),
            headers={"Language":"en-US","User-Agent":"t100-v40-readonly-watch/1.0"},
        )

    async def close(self):
        await self._http.aclose()

    async def _private_get(self,path,params=None):
        if not (
            type(path) is str and
            (path in _EXACT or _EXTERNAL.fullmatch(path)
             or _DEALS.fullmatch(path))
        ):
            raise V40WatchServiceHalted("WATCH_V40_GET_PATH_NOT_ALLOWED")
        if params is not None:
            raise V40WatchServiceHalted("WATCH_V40_GET_QUERY_NOT_ALLOWED")
        stamp=str(int(time.time()*1000))
        sig=hmac.new(
            self._secret.encode(),(self._key+stamp).encode(),
            hashlib.sha256,
        ).hexdigest()
        try:
            response=await self._http.get(path,headers={
                "ApiKey":self._key,"Request-Time":stamp,
                "Signature":sig,"Recv-Window":"10",
            })
            response.raise_for_status()
            envelope=response.json()
        except Exception:
            raise V40WatchServiceHalted("WATCH_V40_GET_TRANSPORT_FAILED") from None
        if not (
            type(envelope) is dict
            and envelope.get("success") is True
            and type(envelope.get("code")) is int
            and envelope["code"]==0
        ):
            raise V40WatchServiceHalted("WATCH_V40_GET_VENUE_REJECTED")
        return envelope.get("data")


@dataclass(frozen=True)
class V40WatchConfiguration:
    scope:V40CrossWatchScope
    journal_path:Path
    scope_sha256:str
    poll_seconds:float
    max_cycles:int|None
    # Never include secrets/Keychain values in reports or repr.


def _positive_decimal(raw:Any)->Decimal|None:
    if type(raw) is not str:
        return None
    try:
        d=Decimal(raw)
    except (ValueError,InvalidOperation):
        return None
    return d if d.is_finite() and d>0 else None


def configure_v40_watch(
    env:Mapping[str,str],*,api_key:str,cycles:int|None,
)->V40WatchConfiguration:
    if (
        env.get("T100_V40_WATCH_ENABLED")!="true"
        or env.get("T100_EXECUTION_MODE")!="paper"
        or env.get("T100_LIVE_ORDER_API_ENABLED")!="false"
        or env.get("T100_LIVE_KILL_SWITCH")!="true"
        or env.get("T100_V40_WATCH_KEYCHAIN_SERVICE")!=KEYCHAIN_READONLY
        or env.get("T100_V40_WATCH_DISCORD_KEYCHAIN_SERVICE")!=KEYCHAIN_DISCORD
    ):
        raise V40WatchServiceHalted("WATCH_V40_PAPER_KILL_AND_SCOPE_REQUIRED")
    expected=env.get("T100_V40_WATCH_API_KEY_SHA256","")
    if (
        type(api_key) is not str or len(api_key)<12
        or type(expected) is not str
        or re.fullmatch(r"[a-f0-9]{64}",expected) is None
        or not hmac.compare_digest(hashlib.sha256(api_key.encode()).hexdigest(),expected)
    ):
        raise V40WatchServiceHalted("WATCH_V40_EXACT_READONLY_KEY_PIN_REQUIRED")
    mode=env.get("T100_V40_WATCH_MODE","")
    scope=V40CrossWatchScope(
        account_key_sha256=expected,mode=mode,
        symbol=env.get("T100_V40_WATCH_SYMBOL") or None,
        position_id=env.get("T100_V40_WATCH_POSITION_ID") or None,
        origin_oid=env.get("T100_V40_WATCH_ORIGIN_OID") or None,
        entry_order_id=env.get("T100_V40_WATCH_ENTRY_ORDER_ID") or None,
        stop_id=env.get("T100_V40_WATCH_STOP_ID") or None,
        contracts=_positive_decimal(env.get("T100_V40_WATCH_CONTRACTS")),
        max_short_stop_price=_positive_decimal(env.get("T100_V40_WATCH_MAX_STOP_PRICE")),
    )
    if not validate_v40_scope(scope):
        raise V40WatchServiceHalted("WATCH_V40_REVIEWED_SCOPE_REQUIRED")
    if cycles is not None and (type(cycles) is not int or cycles<1):
        raise V40WatchServiceHalted("WATCH_V40_CYCLES_INVALID")
    raw=env.get("T100_V40_WATCH_JOURNAL","")
    if type(raw) is not str or not raw.startswith("/"):
        raise V40WatchServiceHalted("WATCH_V40_ABSOLUTE_JOURNAL_REQUIRED")
    journal=Path(raw)
    if journal.is_symlink() or journal.parent.is_symlink():
        raise V40WatchServiceHalted("WATCH_V40_JOURNAL_PATH_UNSAFE")
    # A different immutable scope cannot rearm an old journal; the runtime
    # will also refuse to rewrite an existing journal scope or clear alarms.
    scope_hash=hashlib.sha256(json.dumps({
        "account_key_sha256":scope.account_key_sha256,
        "mode":scope.mode,
        "symbol":scope.symbol,"position_id":scope.position_id,
        "origin_oid":scope.origin_oid,"entry_order_id":scope.entry_order_id,
        "stop_id":scope.stop_id,
        "contracts":str(scope.contracts) if scope.contracts is not None else None,
        "max_short_stop_price":(
            str(scope.max_short_stop_price)
            if scope.max_short_stop_price is not None else None
        ),
    },sort_keys=True,separators=(",",":")).encode()).hexdigest()
    return V40WatchConfiguration(
        scope,journal,scope_hash,1.0,cycles,
    )


async def run_v40_watch(
    *,environ:Mapping[str,str]|None=None,cycles:int|None=1,
):
    env=os.environ if environ is None else environ
    # Explicit Keychain scope must be checked before reading credentials.
    if (
        env.get("T100_V40_WATCH_KEYCHAIN_SERVICE")!=KEYCHAIN_READONLY
        or env.get("T100_V40_WATCH_DISCORD_KEYCHAIN_SERVICE")!=KEYCHAIN_DISCORD
    ):
        raise V40WatchServiceHalted("WATCH_V40_KEYCHAIN_SCOPE_REQUIRED")
    key=_from_keychain(KEYCHAIN_READONLY,KEY_ACCOUNT)
    secret=_from_keychain(KEYCHAIN_READONLY,SECRET_ACCOUNT)
    config=configure_v40_watch(env,api_key=key,cycles=cycles)
    webhook=_from_keychain(KEYCHAIN_DISCORD,DISCORD_ACCOUNT)
    if not re.fullmatch(
        r"https://discord\.com/api/webhooks/[0-9]+/[A-Za-z0-9_-]+",webhook
    ):
        raise V40WatchServiceHalted("WATCH_V40_DEDICATED_DISCORD_INVALID")
    client=V40WatchSignedGetOnly(key,secret)
    notifier=T100LiveOpsNotifier(webhook,cooldown_seconds=0)
    async def send_alert(code:str)->bool:
        if re.fullmatch(r"WATCH_[A-Z0-9_]{1,90}",code) is None:
            return False
        return await notifier.send(
            OpsNotice(
                key="t100-v40-cross-watch-"+code,
                title="T100 V40 isolated GET-only watchdog alert",
                description=code,severity="critical",
            ),force=True,
        )
    try:
        probe=ReadOnlyMexcProbe(client)
        async def observe():
            return await observe_v40_cross_get(probe,config.scope)
        with PilotWatchdogRunner(
            config.journal_path,
            scope_sha256=config.scope_sha256,
            observer=observe,notifier=send_alert,
            poll_seconds=config.poll_seconds,
            observation_timeout_seconds=10.0,
            notification_timeout_seconds=8.0,
        ) as runner:
            return await runner.run(max_cycles=cycles)
    finally:
        await notifier.close()
        await client.close()


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    commands=parser.add_mutually_exclusive_group()
    commands.add_argument("--cycles",type=int,default=1)
    commands.add_argument("--continuous",action="store_true")
    commands.add_argument("--print-sentinel-scope",action="store_true")
    args=parser.parse_args()
    try:
        if args.print_sentinel_scope:
            key=_from_keychain(KEYCHAIN_READONLY,KEY_ACCOUNT)
            config=configure_v40_watch(
                os.environ,api_key=key,cycles=1,
            )
            print("T100_V40_WATCH_SENTINEL_SCOPE_SHA256="+config.scope_sha256)
            print("LOCAL CONFIG ONLY. NO NETWORK OR VENUE ORDERS.")
            return
        result=asyncio.run(run_v40_watch(
            cycles=None if args.continuous else args.cycles,
        ))
        print("T100 V40 READONLY WATCH:",result.status)
        if result.alarm_latched or result.notifications_pending:
            raise SystemExit(2)
    except Exception:
        print("T100 V40 WATCH HALTED_FAIL_CLOSED. NO MEXC ORDERS.")
        raise SystemExit(2)


if __name__=="__main__":
    main()
