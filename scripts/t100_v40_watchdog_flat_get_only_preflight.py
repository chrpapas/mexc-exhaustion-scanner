"""One-shot REAL MEXC signed GET-only dedicated T100 V40 FLAT probe.

No journal creation, no Discord, no position adoption and NO POST to MEXC.
Uses ONLY the new isolated READ-ONLY Keychain service and refuses any live
execution setting. An observed flat account does not attest API key
exchange-side permissions, subaccount identity, position-list completeness,
latency guarantees, native stop execution or readiness to trade.
"""
from __future__ import annotations

import argparse
import asyncio
from dataclasses import asdict,dataclass
import hashlib
import json
import os
import sys
from typing import Mapping

from app.t100_live_readonly_probe import ReadOnlyMexcProbe
from app.t100_v40_cross_watchdog_observer import (
    V40CrossWatchScope,observe_v40_cross_get,
)
from app.t100_v40_cross_watchdog_service import (
    KEYCHAIN_READONLY,V40WatchSignedGetOnly,
)
from scripts.t100_v40_pilot_get_only import (
    KEY_ACCOUNT,SECRET_ACCOUNT,_from_keychain,
)


@dataclass(frozen=True)
class FlatProbeResult:
    status:str
    flat_inventory_observed:bool=False
    account_scope_independently_verified:bool=False
    exchange_side_readonly_permission_verified:bool=False
    account_wide_position_completeness_certified:bool=False
    continuous_monitor_running:bool=False
    no_fresh_signal_required:bool=True
    entry_allowed:bool=False
    live_authorization:bool=False
    mexc_posts:int=0
    database_writes:int=0
    discord_requests:int=0
    journal_writes:int=0
    read_only:bool=True


def _paper_guards(env:Mapping[str,str])->bool:
    return (
        env.get("T100_EXECUTION_MODE")=="paper"
        and env.get("T100_LIVE_ORDER_API_ENABLED")=="false"
        and env.get("T100_LIVE_KILL_SWITCH")=="true"
    )


async def probe_flat(
    *,keychain_service:str,env:Mapping[str,str],
)->FlatProbeResult:
    if keychain_service!=KEYCHAIN_READONLY or not _paper_guards(env):
        return FlatProbeResult("WATCH_V40_FLAT_GET_SCOPE_OR_KILL_INVALID_HALT")
    try:
        key=_from_keychain(KEYCHAIN_READONLY,KEY_ACCOUNT)
        secret=_from_keychain(KEYCHAIN_READONLY,SECRET_ACCOUNT)
        if len(key)<12 or len(secret)<16:
            return FlatProbeResult("WATCH_V40_READONLY_KEYCHAIN_UNVERIFIED_HALT")
    except Exception:
        return FlatProbeResult("WATCH_V40_READONLY_KEYCHAIN_UNVERIFIED_HALT")
    scope=V40CrossWatchScope(
        account_key_sha256=hashlib.sha256(key.encode()).hexdigest(),
        mode="FLAT",
    )
    client=None
    try:
        client=V40WatchSignedGetOnly(key,secret)
        verdict=await observe_v40_cross_get(
            ReadOnlyMexcProbe(client),scope,
        )
        return FlatProbeResult(
            status=verdict.status,
            flat_inventory_observed=(
                verdict.status=="WATCH_FLAT_INVENTORY_OBSERVED_NONAUTHORIZING"
                and verdict.account_flat_observed is True
                and verdict.alert_supervisor is False
            ),
        )
    except Exception:
        return FlatProbeResult("WATCH_V40_FLAT_GET_UNVERIFIED_HALT")
    finally:
        if client is not None:
            await client.close()


def main()->int:
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--keychain-service",required=True)
    args=parser.parse_args()
    if sys.platform!="darwin":
        result=FlatProbeResult("WATCH_V40_MAC_ONLY_HALT")
    else:
        try:
            result=asyncio.run(probe_flat(
                keychain_service=args.keychain_service,env=os.environ,
            ))
        except Exception:
            result=FlatProbeResult("WATCH_V40_FLAT_PROBE_UNKNOWN_HALT")
    print(json.dumps(asdict(result),sort_keys=True))
    print("MEXC_SIGNED_GET_ONLY=true NO_JOURNAL=true NO_DISCORD=true MEXC_POSTS=0")
    return 0 if result.flat_inventory_observed else 2


if __name__=="__main__":
    raise SystemExit(main())
