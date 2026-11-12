"""Dedicated read-only whole-account standby guardian for V40's first trade.

Before the first durable, explicitly enrolled V40 position exists, a
position-ID-pinned guardian cannot run. This account-level sentinel fills
that pre-entry gap: it watches the exact dedicated futures wallet with
two signed GET bookends and reports ANY nonflat account as a critical event.
It NEVER treats an unexpected open position as adopted, protected, or safe.

A separate dedicated Render service may run this monitor every 3-5 seconds.
This module does not create a deployment, enroll an account, write journal
rows, send any MEXC POST, or issue a live execution permit. An independent
durable incident/outbox service remains necessary to alert across crashes.

No shadow T100/225 services, BOXWAVE account, or previous strategy are touched.
"""
from __future__ import annotations

import asyncio
from dataclasses import asdict,dataclass
import json
import os
import ssl
from typing import Any, Awaitable, Callable, Mapping, Protocol

from app.t100_live_guardian_readonly_once import GetOnlyMexcTransport
from app.t100_live_readonly_probe import ReadOnlyMexcProbe


class V40CanaryGuardianHalted(RuntimeError):
    pass


@dataclass(frozen=True)
class V40CanaryGuardianHeartbeat:
    status: str
    monitored_account_flat: bool=False
    inventory_two_signed_get_bookends: bool=False
    hedge_mode_verified: bool=False
    unexpected_positions: int=0
    unexpected_stops: int=0
    unexpected_orders: int=0
    incident_alert: bool=True
    independent_durable_alert_delivery: bool=False
    running_guardian_certified: bool=False
    entry_allowed: bool=False
    live_authorization: bool=False
    emergency_close_allowed: bool=False
    mexc_posts: int=0
    database_writes: int=0


class AccountProbe(Protocol):
    async def raw_open_positions(self)->list[dict[str,Any]]: ...
    async def raw_open_stops(self)->list[dict[str,Any]]: ...
    async def raw_open_order_counts(self)->dict[str,int]: ...
    async def raw_position_mode(self)->int: ...


async def _inventory(probe:AccountProbe) -> tuple[Any,...]:
    return (
        await probe.raw_open_positions(),
        await probe.raw_open_stops(),
        await probe.raw_open_order_counts(),
        await probe.raw_position_mode(),
    )


def _count_orders(counts:Any)->int|None:
    if type(counts) is not dict:
        return None
    keys=("sumCount","stopOrderCount","limitOrderCount",
          "planOrderCount","trackOrderCount")
    if any(type(counts.get(k)) is not int or counts[k]<0 for k in keys):
        return None
    total=sum(counts[k] for k in keys[1:])
    return counts["sumCount"] if counts["sumCount"]==total else None


async def observe_v40_flat_guardian(probe:AccountProbe)->V40CanaryGuardianHeartbeat:
    """No journal, no MEXC mutation: stable full-account flat readback only.

    A dirty account triggers operator review, NOT a blind emergency close.
    Position ownership must be established from immutable filled entry +
    checkpoint records by the separately enrolled owned-position guardian.
    """
    fail=lambda s:V40CanaryGuardianHeartbeat(status=s)
    try:
        first=await _inventory(probe)
        second=await _inventory(probe)
    except Exception:
        return fail("V40_GUARDIAN_SIGNED_GET_UNAVAILABLE_HALT")
    for row in (first,second):
        if (
            type(row[0]) is not list or type(row[1]) is not list
            or any(type(x) is not dict for x in row[0]+row[1])
            or _count_orders(row[2]) is None
            or type(row[3]) is not int or row[3]!=1
        ):
            return fail("V40_GUARDIAN_ACCOUNT_SHAPE_OR_HEDGE_UNVERIFIED_HALT")
    if first!=second:
        return fail("V40_GUARDIAN_ACCOUNT_BOOKENDS_DRIFT_HALT")
    positions,stops,orders,mode=second
    if positions or stops or orders["sumCount"]!=0:
        return V40CanaryGuardianHeartbeat(
            status="V40_GUARDIAN_NONFLAT_OR_PENDING_ORDER_UNKNOWN_OWNER_HALT",
            unexpected_positions=len(positions),
            unexpected_stops=len(stops),
            unexpected_orders=orders["sumCount"],
            inventory_two_signed_get_bookends=True,
            hedge_mode_verified=True,
        )
    return V40CanaryGuardianHeartbeat(
        status="V40_GUARDIAN_FLAT_ACCOUNT_DOUBLE_GET_STANDBY_NONAUTHORIZING",
        monitored_account_flat=True,
        inventory_two_signed_get_bookends=True,
        hedge_mode_verified=True,
        unexpected_positions=0,unexpected_stops=0,unexpected_orders=0,
        incident_alert=False,
    )


def verify_guardian_environment(env:Mapping[str,str])->None:
    """Explicitly separate this read-only key from any live POST-capable key.

    This is a stand-by observation process only: it cannot certify the
    account before first trade is enrolled, and MUST NOT be used as a
    substitute for continuous native stop monitoring of a managed position.
    """
    if (
        env.get("T100_V40_CANARY_GUARDIAN_ENABLED")!="true"
        or env.get("T100_EXECUTION_MODE","paper")!="paper"
        or env.get("T100_LIVE_ORDER_API_ENABLED","false")!="false"
        or env.get("T100_LIVE_KILL_SWITCH","true")!="true"
        or env.get("T100_GUARDIAN_OBSERVER_ENABLED")!="true"
        or env.get("T100_V40_GUARDIAN_ACCOUNT_SCOPE")!="V40_DEDICATED"
    ):
        raise V40CanaryGuardianHalted("V40_GUARDIAN_READONLY_ACCOUNT_SCOPE_REQUIRED")
    key=env.get("T100_V40_GUARDIAN_READONLY_API_KEY","")
    secret=env.get("T100_V40_GUARDIAN_READONLY_API_SECRET","")
    if (
        type(key) is not str or len(key)<12 or key!=key.strip()
        or type(secret) is not str or len(secret)<16 or secret!=secret.strip()
    ):
        raise V40CanaryGuardianHalted("V40_GUARDIAN_READONLY_CREDENTIALS_MISSING")
    if (
        key==env.get("T100_V40_TRADER_API_KEY")
        and env.get("T100_V40_TRADER_API_KEY")
    ):
        raise V40CanaryGuardianHalted("V40_GUARDIAN_MUST_HAVE_SEPARATE_GET_KEY")


async def observe_v40_account_once(
    env:Mapping[str,str],
)->V40CanaryGuardianHeartbeat:
    try:
        verify_guardian_environment(env)
    except Exception:
        return V40CanaryGuardianHeartbeat(
            "V40_GUARDIAN_READONLY_SCOPE_OR_KEY_INVALID_HALT"
        )
    import httpx
    try:
        async with httpx.AsyncClient(
            base_url=GetOnlyMexcTransport.BASE_URL,
            trust_env=False,follow_redirects=False,
            timeout=httpx.Timeout(8.0),
        ) as client:
            raw=GetOnlyMexcTransport(
                client,api_key=env["T100_V40_GUARDIAN_READONLY_API_KEY"],
                api_secret=env["T100_V40_GUARDIAN_READONLY_API_SECRET"],
            )
            return await asyncio.wait_for(
                observe_v40_flat_guardian(ReadOnlyMexcProbe(raw)),
                timeout=24.0,
            )
    except Exception:
        return V40CanaryGuardianHeartbeat(
            "V40_GUARDIAN_ACCOUNT_NETWORK_OR_TIMEOUT_HALT"
        )


async def run_v40_guardian(
    env:Mapping[str,str],
    *,
    observe:Callable[[Mapping[str,str]],Awaitable[V40CanaryGuardianHeartbeat]]=
       observe_v40_account_once,
    pause:Callable[[float],Awaitable[Any]]=asyncio.sleep,
    emit:Callable[[str],object]=print,
    max_cycles:int|None=None,
)->int:
    """Live GET-only health loop; return nonzero on first nonflat/anomaly.

    A manager must restart this observer and deliver durable notifications
    independently of the trading worker. No account mutations are possible.
    """
    try:
        verify_guardian_environment(env)
    except Exception:
        emit(json.dumps(asdict(V40CanaryGuardianHeartbeat(
            "V40_GUARDIAN_READONLY_SCOPE_OR_KEY_INVALID_HALT"
        )),sort_keys=True))
        return 2
    interval=env.get("T100_V40_CANARY_GUARDIAN_INTERVAL_SECONDS","5")
    if interval not in ("3","4","5") or (
        max_cycles is not None and
        (type(max_cycles) is not int or not 1<=max_cycles<=10000)
    ):
        return 2
    cycle=0
    while max_cycles is None or cycle<max_cycles:
        cycle+=1
        try:
            state=await asyncio.wait_for(observe(env),timeout=27.0)
        except Exception:
            state=V40CanaryGuardianHeartbeat(
                "V40_GUARDIAN_OBSERVER_ERROR_OR_TIMEOUT_HALT"
            )
        if type(state) is not V40CanaryGuardianHeartbeat:
            state=V40CanaryGuardianHeartbeat(
                "V40_GUARDIAN_OBSERVER_REPORT_UNVERIFIED_HALT"
            )
        emit(json.dumps(asdict(state),sort_keys=True))
        if (
            state.status!="V40_GUARDIAN_FLAT_ACCOUNT_DOUBLE_GET_STANDBY_NONAUTHORIZING"
            or state.incident_alert is not False
            or state.monitored_account_flat is not True
            or state.mexc_posts!=0
            or state.database_writes!=0
            or state.entry_allowed is not False
            or state.live_authorization is not False
        ):
            return 2
        if max_cycles is not None and cycle>=max_cycles:
            return 0
        await pause(float(interval))
    return 0


def main()->int:
    import argparse
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run",action="store_true")
    parser.add_argument("--once",action="store_true")
    a=parser.parse_args()
    if not a.run:
        print(json.dumps(asdict(V40CanaryGuardianHeartbeat(
            "V40_GUARDIAN_OFF_NOT_STARTED"
        )),sort_keys=True))
        return 0
    return asyncio.run(run_v40_guardian(
        dict(os.environ),max_cycles=1 if a.once else None,
    ))


if __name__=="__main__":
    raise SystemExit(main())
