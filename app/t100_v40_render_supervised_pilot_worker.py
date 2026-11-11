"""Continuously observe the REAL frozen V40 scanner and isolated MEXC pilot.

Strict first-stage activation of the genuine strategy feed; **NOT** a live
execution adapter. Always requires the existing paper/kill-latched V40
worker. Does not generate fake BTC signals, POST orders, change stops or
touch scanner/BOXWAVE. Only a *fresh* validated scanner signal can appear.

Every cycle:
  1. Fresh production PostgreSQL Stage2 read using only the independently
     audited column-level read-only t100_v40_signal_reader login.
  2. Frozen V40 VOL30 and causal 49-bar economics from the actual signal.
  3. TWO signed account-wide MEXC GET bookends even when source is empty;
     refuse unexpected positions, stop orders, pending orders or nonflat
     equity before proceeding to a later cycle.
  4. Structured bounded logs. Candidate remains NONAUTHORIZING: the native
     stop/trailing/fill/recovery runtime is NOT connected or certified.

Source and account failure halt the process; Render's normal restart
behavior never upgrades authorizations. No database writes, no POST.
"""
from __future__ import annotations

import asyncio
from dataclasses import asdict, dataclass
from decimal import Decimal, InvalidOperation
import json
import os
from typing import Any, Awaitable, Callable, Mapping

from app.t100_v40_render_signal_intake import (
    V40SignalIntakeReport, _once, eligible_environment,
)
from scripts.t100_v40_pilot_get_only import PilotGetOnlyResult, observe_live_pilot

ACTIVATION_FLAG="T100_V40_REAL_SIGNAL_MONITOR_ENABLED"
POLL_SECONDS=60
MAX_CYCLE_SECONDS=50


@dataclass(frozen=True)
class V40SupervisedPilotObservation:
    status: str
    source_status: str | None = None
    signal_id: int | None = None
    signal_symbol: str | None = None
    fresh_signal_count: int = 0
    account_equity_usdt: str | None = None
    account_flat_verified: bool = False
    source_verified: bool = False
    real_signal_economics_observed: bool = False
    enabled_position_capacity: int = 0
    live_adapter_connected: bool = False
    native_stop_management_connected: bool = False
    continuous_nonflat_guardian_connected: bool = False
    mexc_orders_sent: int = 0
    database_writes: int = 0
    live_authorization: bool = False
    entry_allowed: bool = False


def monitor_environment(env: Mapping[str,str]) -> bool:
    return (
        eligible_environment(env)
        and env.get(ACTIVATION_FLAG)=="true"
        and env.get("T100_V40_SINGLE_POSITION_PILOT")=="true"
    )


def _positive_usdt(value:Any)->bool:
    try:
        n=Decimal(str(value))
    except (InvalidOperation,TypeError,ValueError):
        return False
    return n.is_finite() and n>0


async def supervise_real_v40_cycle(
    env: Mapping[str,str], *,
    signal_read: Callable[[Mapping[str,str]], Awaitable[V40SignalIntakeReport]]=_once,
    account_observe: Callable[...,Awaitable[PilotGetOnlyResult]]=observe_live_pilot,
) -> V40SupervisedPilotObservation:
    def blocked(status:str, source:V40SignalIntakeReport|None=None):
        return V40SupervisedPilotObservation(
            status=status,
            source_status=source.status if source else None,
            signal_id=source.signal_id if source else None,
            signal_symbol=source.symbol if source else None,
            fresh_signal_count=source.signal_count if source else 0,
            source_verified=source.fresh_source_verified if source else False,
        )
    if not monitor_environment(env):
        return blocked("V40_REAL_SIGNAL_MONITOR_PAPER_KILL_OR_SCOPE_REQUIRED")
    try:
        source=await asyncio.wait_for(signal_read(env),timeout=27)
    except Exception:
        return blocked("V40_REAL_SIGNAL_SOURCE_OR_TIMEOUT_HALT")
    if (
        type(source) is not V40SignalIntakeReport
        or source.fresh_source_verified is not True
        or source.mexc_posts!=0 or source.database_writes!=0
        or source.live_authorization is not False
        or source.entry_allowed is not False
    ):
        return blocked("V40_REAL_SIGNAL_SOURCE_UNVERIFIED_HALT")
    allowed_status=(
        "V40_SIGNAL_INTAKE_FRESH_EMPTY_SOURCE_OBSERVED",
        "V40_GENUINE_SIGNAL_AND_FLAT_ACCOUNT_MODELED_NONAUTHORIZING",
    )
    if source.status not in allowed_status:
        return blocked("V40_REAL_SIGNAL_SOURCE_OR_CANDIDATE_HALT",source)
    if source.status==allowed_status[1] and (
        type(source.signal_id) is not int or source.signal_id<=0
        or type(source.symbol) is not str or not source.symbol.endswith("_USDT")
        or source.signal_count!=1
        or not _positive_usdt(source.theoretical_v40_notional_usdt)
        or source.current_flat_account_verified is not True
    ):
        return blocked("V40_REAL_SIGNAL_CANDIDATE_ECONOMICS_HALT",source)
    if source.status==allowed_status[0] and (
        source.signal_count!=0 or source.signal_id is not None
        or source.symbol is not None
    ):
        return blocked("V40_REAL_SIGNAL_EMPTY_SOURCE_SHAPE_HALT",source)

    key,secret=env.get("MEXC_API_KEY",""),env.get("MEXC_API_SECRET","")
    if (
        type(key) is not str or len(key)<12
        or type(secret) is not str or len(secret)<16
    ):
        return blocked("V40_REAL_SIGNAL_ACCOUNT_KEYS_MISSING",source)
    try:
        account=await asyncio.wait_for(
            account_observe(key=key,secret=secret),timeout=23,
        )
    except Exception:
        # Open positions, unsupported margin, missing stops and HTTP errors
        # are all safety halts. Never assume that an empty source means flat.
        return blocked("V40_REAL_SIGNAL_ACCOUNT_NOT_FLAT_OR_GET_FAILED_HALT",source)
    if not (
        type(account) is PilotGetOnlyResult
        and account.status=="PILOT_FLAT_HEDGE_SIGNED_GET_BOOKENDS_NONAUTHORIZING"
        and account.samples==2
        and account.position_count==0
        and account.native_stop_count==0
        and account.inventory_bookends_equal is True
        and account.hedge_mode_observed is True
        and account.mexc_posts==0 and account.db_mutations==0
        and account.live_authorization is False
        and _positive_usdt(account.account_equity_usdt)
    ):
        return blocked("V40_REAL_SIGNAL_ACCOUNT_INVENTORY_UNVERIFIED_HALT",source)
    if source.status==allowed_status[1] and (
        source.account_equity_usdt is None
        or abs(Decimal(source.account_equity_usdt) -
               Decimal(str(account.account_equity_usdt)))>Decimal("0.00000001")
    ):
        return blocked("V40_REAL_SIGNAL_ACCOUNT_EQUITY_DRIFT_HALT",source)
    return V40SupervisedPilotObservation(
        status=(
            "V40_REAL_FRESH_SIGNAL_OBSERVED_EXECUTION_STILL_GUARDED"
            if source.signal_id is not None else
            "V40_REAL_SCANNER_ACTIVE_NO_CANDIDATE_ACCOUNT_FLAT"
        ),
        source_status=source.status,
        signal_id=source.signal_id,
        signal_symbol=source.symbol,
        fresh_signal_count=source.signal_count,
        account_equity_usdt=str(account.account_equity_usdt),
        account_flat_verified=True,
        source_verified=True,
        real_signal_economics_observed=source.signal_id is not None,
    )


async def run_supervised_signal_monitor(
    env:Mapping[str,str], *, max_cycles:int|None=None,
    observe_cycle:Callable[...,Awaitable[V40SupervisedPilotObservation]]=supervise_real_v40_cycle,
    delay: Callable[[float],Awaitable[Any]]=asyncio.sleep,
)->int:
    if not monitor_environment(env):
        print(json.dumps(asdict(V40SupervisedPilotObservation(
            "V40_REAL_SIGNAL_MONITOR_PAPER_KILL_OR_SCOPE_REQUIRED"
        )),sort_keys=True),flush=True)
        return 2
    if max_cycles is not None and (type(max_cycles) is not int or max_cycles<1):
        return 2
    last_signal=None
    number=0
    while max_cycles is None or number<max_cycles:
        try:
            result=await asyncio.wait_for(
                observe_cycle(env),timeout=MAX_CYCLE_SECONDS,
            )
        except Exception:
            result=V40SupervisedPilotObservation("V40_REAL_SIGNAL_MONITOR_CYCLE_TIMEOUT_OR_ERROR_HALT")
        number+=1
        ok=(
            type(result) is V40SupervisedPilotObservation
            and result.status in (
                "V40_REAL_SCANNER_ACTIVE_NO_CANDIDATE_ACCOUNT_FLAT",
                "V40_REAL_FRESH_SIGNAL_OBSERVED_EXECUTION_STILL_GUARDED",
            )
            and result.source_verified is True
            and result.account_flat_verified is True
            and result.mexc_orders_sent==0 and result.database_writes==0
            and result.live_authorization is False and result.entry_allowed is False
        )
        # Explicit heartbeat every 5 cycles and every genuinely new signal.
        if not ok or number==1 or number%5==0 or (
            result.signal_id is not None and result.signal_id!=last_signal
        ):
            print(json.dumps(asdict(result),sort_keys=True),flush=True)
        if not ok:
            return 2
        last_signal=result.signal_id if result.signal_id is not None else last_signal
        if max_cycles is None or number<max_cycles:
            await delay(POLL_SECONDS)
    return 0


def main()->int:
    try:
        return asyncio.run(run_supervised_signal_monitor(dict(os.environ)))
    except (KeyboardInterrupt,SystemExit):
        return 0


if __name__=="__main__":
    raise SystemExit(main())
