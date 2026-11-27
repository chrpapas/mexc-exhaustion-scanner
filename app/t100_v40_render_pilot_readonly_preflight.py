"""Isolated one-shot Render V40 pilot GET-only account/economics preflight.

This tool is NOT the scanner, guardian, strategy executor or order transport.
Run manually in a dedicated, unprivileged, isolated Render worker with a new
READ-ONLY API key for the exact dedicated V40 WEB pilot subaccount.

Proves only two stable signed GET snapshots of empty inventory, hedge mode
and flat USDT assets. A key fingerprint is not proof of the correct exchange
account or exchange-side READ-only permission. NEVER uses an existing
scanner/DB owner credential, any trader key, or BOXWAVE credentials.

No MEXC POST, order, stop, DB access/write, SQLite file or Discord call.
All failures return fixed redacted statuses. Do NOT configure this --once
command as a continuously running worker (Render may restart on exit).
"""
from __future__ import annotations

import argparse
import asyncio
from dataclasses import asdict,dataclass
from decimal import Decimal as D, InvalidOperation
import hashlib
import hmac
import json
import os
import re
from typing import Any, Awaitable, Callable, Mapping

from scripts.t100_v40_pilot_get_only import (
    PilotGetOnlyResult, observe_live_pilot,
)


@dataclass(frozen=True)
class V40RenderPilotReadiness:
    status:str
    stable_flat_inventory_verified:bool=False
    usdt_account_economics_observed:bool=False
    pilot_notional_ceiling_usdt:str|None=None
    account_key_fingerprint_matched:bool=False
    account_subaccount_identity_independently_verified:bool=False
    exchange_side_readonly_permission_verified:bool=False
    current_contract_lot_and_liquidity_verified:bool=False
    fresh_v40_signal_verified:bool=False
    cross_collateral_and_risk_verified:bool=False
    native_v40_short_stop_lifecycle_verified:bool=False
    autonomous_v40_adapter_certified:bool=False
    independent_nonflat_watchdog_verified:bool=False
    entry_allowed:bool=False
    live_authorization:bool=False
    mexc_posts:int=0
    database_writes:int=0
    discord_requests:int=0
    journal_writes:int=0


def _guards(env:Mapping[str,str])->bool:
    return all((
        env.get("T100_V40_RENDER_PILOT_PREFLIGHT_ENABLED")=="true",
        env.get("T100_EXECUTION_MODE")=="paper",
        env.get("T100_LIVE_ORDER_API_ENABLED")=="false",
        env.get("T100_LIVE_KILL_SWITCH")=="true",
        env.get("EXECUTION_ENABLED")=="false",
        env.get("TRADING_MODE")=="paper",
        env.get("T100_V40_PILOT_ROLE")=="ISOLATED_READONLY_PREFLIGHT",
    ))


async def run_once(
    env:Mapping[str,str], *,
    observe:Callable[...,Awaitable[PilotGetOnlyResult]]=observe_live_pilot,
)->V40RenderPilotReadiness:
    if not _guards(env):
        return V40RenderPilotReadiness("V40_RENDER_PILOT_PAPER_KILL_SCOPE_HALT")
    key=env.get("T100_V40_PILOT_READONLY_API_KEY","")
    secret=env.get("T100_V40_PILOT_READONLY_API_SECRET","")
    pin=env.get("T100_V40_PILOT_READONLY_KEY_SHA256","")
    if (
        type(key) is not str or len(key)<12
        or type(secret) is not str or len(secret)<16
        or type(pin) is not str or re.fullmatch(r"[a-f0-9]{64}",pin) is None
        or not hmac.compare_digest(
            hashlib.sha256(key.encode()).hexdigest(),pin,
        )
    ):
        return V40RenderPilotReadiness("V40_RENDER_PILOT_EXACT_READONLY_KEY_PIN_HALT")
    try:
        sample=await asyncio.wait_for(
            observe(key=key,secret=secret),timeout=25,
        )
        if (
            type(sample) is not PilotGetOnlyResult
            or sample.status!="PILOT_FLAT_HEDGE_SIGNED_GET_BOOKENDS_NONAUTHORIZING"
            or sample.samples!=2
            or sample.position_count!=0
            or sample.native_stop_count!=0
            or sample.hedge_mode_observed is not True
            or sample.inventory_bookends_equal is not True
            or sample.mexc_posts!=0
            or sample.db_mutations!=0
            or sample.live_authorization is not False
        ):
            return V40RenderPilotReadiness("V40_RENDER_PILOT_INVENTORY_UNVERIFIED_HALT")
        equity=D(sample.account_equity_usdt)
        available=D(sample.available_balance_usdt)
        if (
            not equity.is_finite() or equity<=0
            or not available.is_finite() or available<=0
            or available>equity+D("0.00000001")
        ):
            return V40RenderPilotReadiness("V40_RENDER_PILOT_ECONOMICS_UNVERIFIED_HALT")
        # Research maximum only. NOT an executable quantity, loss cap or permit.
        cap=min(D("12.50"),(min(equity,available)*D("0.25")))
        if cap<=0:
            return V40RenderPilotReadiness("V40_RENDER_PILOT_ZERO_BUDGET_HALT")
        return V40RenderPilotReadiness(
            "V40_RENDER_PILOT_FLAT_ECONOMICS_READ_ONLY_NONAUTHORIZING",
            stable_flat_inventory_verified=True,
            usdt_account_economics_observed=True,
            pilot_notional_ceiling_usdt=str(cap.quantize(D("0.00000001"))),
            account_key_fingerprint_matched=True,
        )
    except Exception:
        # Never reflect raw venue/client exceptions or environment secrets.
        return V40RenderPilotReadiness("V40_RENDER_PILOT_READ_ONLY_GET_FAILED_HALT")


def main()->int:
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--once",action="store_true",required=True)
    parser.parse_args()
    try:
        report=asyncio.run(run_once(os.environ))
    except Exception:
        report=V40RenderPilotReadiness("V40_RENDER_PILOT_UNEXPECTED_HALT")
    print(json.dumps(asdict(report),sort_keys=True))
    print("MEXC_GET_ONLY=true MEXC_POSTS=0 DB_WRITES=0 DISCORD_POSTS=0")
    return 0 if report.stable_flat_inventory_verified else 2


if __name__=="__main__":
    raise SystemExit(main())
