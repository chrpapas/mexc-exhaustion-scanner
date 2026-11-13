"""GET/SELECT-only evidence collector for an emergency V40 missing native SL.

Uses only the same signed, hard-scoped dedicated pilot GET key as the
executor and the restricted V40 runtime DB reader. Never sends close orders,
sets prices, creates a DB row or grants entry authorization. The single
target must come from exact immutable entry/position/stop/intent provenance.
"""
from __future__ import annotations

import asyncio
from dataclasses import dataclass
from decimal import Decimal as D
import time
from typing import Any,Awaitable,Callable,Protocol

from app.t100_live_db_inspect import EXPECTED_DATABASE
from app.t100_live_role_preflight import TARGET_ROLE
from app.t100_live_four_slot_ledger_reader import PostgresFourSlotLedgerReader
from app.t100_live_readonly_probe import ReadOnlyMexcProbe
from app.t100_v40_emergency_close import (
    V40EmergencyCloseTarget,V40ExitFrame,V40EmergencyCloseHalted,
    attest_missing_native_stop_close,
)

_DEPTH="/api/v1/contract/depth/"
_MAX_CYCLE_MS=14000
_MAX_BOOK_AGE_MS=1500
_MAX_QUOTE_CLOCK_LEAD_MS=100


class PublicRead(Protocol):
    async def get(self,path:str,*,params:Any)->Any: ...


@dataclass(frozen=True)
class V40EmergencyObservation:
    status:str
    target:V40EmergencyCloseTarget|None=None
    close_post_attempted:bool=False
    database_writes:int=0
    entry_allowed:bool=False
    live_authorization:bool=False


async def _signed_frame(
    probe:ReadOnlyMexcProbe,symbol:str,
)->V40ExitFrame:
    return V40ExitFrame(
        positions=await probe.raw_open_positions(),
        symbol_positions=await probe.raw_open_positions(symbol),
        stops=await probe.raw_open_stops(),
        symbol_stops=await probe.raw_open_stops(symbol),
        counts=await probe.raw_open_order_counts(),
        hedge_mode=await probe.raw_position_mode(),
    )


async def _safe_quote(
    *,public:PublicRead,symbol:str,clock_ms:Callable[[],int],
)->D:
    from app.t100_live_v40_candidate_market_witness import _level_rows
    try:
        response=await public.get(_DEPTH+symbol,params={"limit":"100"})
        response.raise_for_status()
        raw=response.json()
        if (
            type(raw) is not dict
            or raw.get("success") is not True
            or raw.get("code")!=0
            or type(raw.get("data")) is not dict
        ):
            raise ValueError
        depth=raw["data"]
        now=clock_ms()
        ms=depth.get("timestamp")
        if (
            type(now) is not int or type(ms) is not int
            or not 0<=now-ms<=_MAX_BOOK_AGE_MS
            or type(depth.get("version")) is not int
            or depth["version"]<=0
        ):
            raise ValueError
        asks=_level_rows(depth.get("asks"),side="buy")
        bids=_level_rows(depth.get("bids"),side="sell")
        if bids[0][0]>=asks[0][0]:
            raise ValueError
        return asks[0][0]
    except Exception:
        raise V40EmergencyCloseHalted(
            "V40_EMERGENCY_CLOSE_FRESH_PUBLIC_MARKET_ASK_UNAVAILABLE"
        ) from None


async def _read_exact_original_notional(
    *,pool:Any,entry_oid:str,
)->D:
    try:
        async with pool.acquire() as conn:
            async with conn.transaction(
                isolation="repeatable_read",readonly=True,
            ):
                if (
                    await conn.fetchval("SELECT current_database()")
                        !=EXPECTED_DATABASE
                    or await conn.fetchval("SELECT current_user")
                        !=TARGET_ROLE
                    or await conn.fetchval(
                        "SELECT current_setting('transaction_read_only')"
                    )!="on"
                ):
                    raise ValueError
                row=await conn.fetchrow(
                    """SELECT notional_usdt,state,external_oid
                       FROM public.t100_live_order_intents
                       WHERE external_oid=$1""",
                    entry_oid,
                )
                if (
                    row is None or row["state"]!="ACK"
                    or row["external_oid"]!=entry_oid
                ):
                    raise ValueError
                amount=D(str(row["notional_usdt"]))
                if not amount.is_finite() or not D(0)<amount<=D("12.50"):
                    raise ValueError
                return amount
    except Exception:
        raise V40EmergencyCloseHalted(
            "V40_EMERGENCY_CLOSE_EXACT_NOTIONAL_DB_UNAVAILABLE"
        ) from None


async def observe_emergency_target(
    *,pool:Any,probe:ReadOnlyMexcProbe,public:PublicRead,
    clock_ms:Callable[[],int]|None=None,
    delay:Callable[[float],Awaitable[Any]]=asyncio.sleep,
)->V40EmergencyObservation:
    """One bounded independent window. Missing stop is never invented."""
    if (
        pool is None or type(probe) is not ReadOnlyMexcProbe
        or public is None
    ):
        return V40EmergencyObservation(
            "V40_EMERGENCY_CLOSE_READERS_MISSING"
        )
    clock=clock_ms or (lambda:int(time.time()*1000))
    started=clock()
    try:
        ledger=await PostgresFourSlotLedgerReader(pool).read_snapshot()
        if len(ledger.checkpoints)!=1:
            raise V40EmergencyCloseHalted(
                "V40_EMERGENCY_CLOSE_NO_SINGLE_DURABLE_OWNER"
            )
        origin=ledger.checkpoints[0]
        if type(origin) is not dict:
            raise V40EmergencyCloseHalted(
                "V40_EMERGENCY_CLOSE_CHECKPOINT_INVALID"
            )
        symbol=origin.get("symbol")
        oid=origin.get("external_oid")
        if type(symbol) is not str or type(oid) is not str:
            raise V40EmergencyCloseHalted(
                "V40_EMERGENCY_CLOSE_CHECKPOINT_INVALID"
            )
        frame1=await _signed_frame(probe,symbol)
        original_order=await probe.raw_order_by_external_oid(symbol,oid)
        notional=await _read_exact_original_notional(
            pool=pool,entry_oid=oid,
        )
        await delay(.3)
        frame2=await _signed_frame(probe,symbol)
        ask=await _safe_quote(
            public=public,symbol=symbol,clock_ms=clock,
        )
        ended=clock()
        if (
            type(started) is not int or type(ended) is not int
            or ended<started or ended-started>_MAX_CYCLE_MS
        ):
            raise V40EmergencyCloseHalted(
                "V40_EMERGENCY_CLOSE_SIGNED_WINDOW_TOO_STALE"
            )
        target=attest_missing_native_stop_close(
            ledger=ledger,first=frame1,second=frame2,
            origin_order=original_order,reference_ask=ask,
            original_notional_usdt=notional,
        )
        return V40EmergencyObservation(
            "V40_EMERGENCY_OWNED_SHORT_MISSING_NATIVE_SL_TWO_GETS",
            target=target,
        )
    except V40EmergencyCloseHalted as exc:
        return V40EmergencyObservation(str(exc))
    except Exception:
        return V40EmergencyObservation(
            "V40_EMERGENCY_CLOSE_LEDGER_OR_SIGNED_GET_FAILED"
        )
