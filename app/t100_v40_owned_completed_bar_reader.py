"""Real scanner Min30 + durable cursor owned-position bar projection (GET/SELECT).

One *exactly consecutive* completed 30-minute bar per independently owned
position, or a positively established not-yet-due cursor. Never skip missing
history or use an in-progress candle. Never manufacture the post-fill low.

Scanner and runtime SQL roles are distinct, independently pinned, READ ONLY.
Current price ticks are from the public MEXC contract detail response, not
from historical lot metadata. Any absence/ambiguity HALTS the entire batch.

No DB writes, MEXC POST, orders, native stop changes, or live authorization.
A successful bar projection alone is NOT a release or liquidation proof.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal as D, InvalidOperation
from typing import Any, Mapping, Sequence

from app.t100_live_db_inspect import EXPECTED_DATABASE
from app.t100_live_role_preflight import TARGET_ROLE
from app.t100_live_v40_stage2_source import (
    RESTRICTED_V40_SIGNAL_READER_ROLE,
)
from app.t100_v40_unreleased_bar_cursor import (
    V40BarCursorState,V40DurableBarCursor,
)
from app.t100_v40_unreleased_exit_policy import (
    V40CompletedBar,V40ExitPolicyHalted,
)

BAR_SECONDS=1800
MAX_SLOTS=4
MIN_CLOSE_FINALITY_SECONDS=8


class V40OwnedBarsHalted(RuntimeError):
    """One fixed reason, never a database error, symbol or account identifier."""


@dataclass(frozen=True)
class V40OwnedDueBarWindow:
    status: str
    cursors: tuple[V40BarCursorState,...]
    bars: dict[str,V40CompletedBar]
    ticks: dict[str,D]
    no_due_position_ids: tuple[str,...]
    latest_safe_open_ts: int
    scanner_role_verified: bool
    runtime_role_verified: bool
    exchange_public_specs_observed: bool
    next_entry_allowed: bool = False
    live_authorization: bool = False
    mexc_posts: int = 0
    database_writes: int = 0


def _symbol(value: object) -> bool:
    return (
        type(value) is str
        and re.fullmatch(r"[A-Z0-9]+_USDT",value) is not None
        and value not in ("BTC_USDT","ETH_USDT")
    )


def _positive(value: object) -> D:
    if value is None or type(value) is bool:
        raise V40OwnedBarsHalted("V40_OWNED_BAR_EXCHANGE_NUMBER_INVALID")
    try:
        d=D(str(value))
    except (ValueError,TypeError,InvalidOperation):
        raise V40OwnedBarsHalted("V40_OWNED_BAR_EXCHANGE_NUMBER_INVALID") from None
    if not d.is_finite() or d<=0:
        raise V40OwnedBarsHalted("V40_OWNED_BAR_EXCHANGE_NUMBER_INVALID")
    return d


def next_due_open_ts(cursor: V40BarCursorState) -> int:
    if type(cursor) is not V40BarCursorState or cursor.pending_open_ts is not None:
        raise V40OwnedBarsHalted("V40_OWNED_BAR_CURSOR_PENDING_OR_INVALID")
    # First eligible bar opens *after* independently observed fill time.
    # Ceil milliseconds to the next exact Min30 open, excluding a partial
    # entry bar even when the fill is a few ms after a new bar begins.
    first=((cursor.signed_fill_at_ms+BAR_SECONDS*1000-1)//(BAR_SECONDS*1000))*BAR_SECONDS
    if cursor.last_committed_open_ts is None:
        return first
    if (
        type(cursor.last_committed_open_ts) is not int
        or cursor.last_committed_open_ts % BAR_SECONDS!=0
        or cursor.last_committed_open_ts<first
    ):
        raise V40OwnedBarsHalted("V40_OWNED_BAR_CURSOR_CHRONOLOGY_INVALID")
    return cursor.last_committed_open_ts+BAR_SECONDS


def _bar_from_scanner_row(row: object, *,expected_ts:int, now_ts:int) -> V40CompletedBar:
    if not isinstance(row,dict):
        raise V40OwnedBarsHalted("V40_OWNED_BAR_SCANNER_ROW_INVALID")
    stamp=row.get("open_time")
    if (
        type(stamp) is not datetime
        or stamp.tzinfo is None or stamp.utcoffset() is None
        or stamp.astimezone(timezone.utc).microsecond!=0
        or int(stamp.timestamp())!=expected_ts
    ):
        raise V40OwnedBarsHalted("V40_OWNED_BAR_EXACT_OPEN_TIMESTAMP_INVALID")
    try:
        bar=V40CompletedBar(
            open_ts=expected_ts,
            open=_positive(row.get("open")),high=_positive(row.get("high")),
            low=_positive(row.get("low")),close=_positive(row.get("close")),
        )
        bar.validate(now_ts)
    except (V40ExitPolicyHalted,V40OwnedBarsHalted):
        raise V40OwnedBarsHalted("V40_OWNED_BAR_SCANNER_OHLC_INVALID") from None
    return bar


def _spec_tick(data:object,*,symbol:str)->D:
    rows=data if type(data) is list else [data]
    matching=[x for x in rows if type(x) is dict and x.get("symbol")==symbol]
    if len(matching)!=1:
        raise V40OwnedBarsHalted("V40_OWNED_BAR_PUBLIC_CONTRACT_NOT_UNIQUE")
    row=matching[0]
    if row.get("apiAllowed") is not True:
        raise V40OwnedBarsHalted("V40_OWNED_BAR_CONTRACT_NOT_API_ALLOWED")
    return _positive(row.get("priceUnit"))


class PostgresV40OwnedBarReader:
    def __init__(
        self,*,scanner_pool:Any,runtime_pool:Any,public_get:Any,
    ) -> None:
        if (
            scanner_pool is None or runtime_pool is None
            or scanner_pool is runtime_pool
            or not callable(getattr(scanner_pool,"acquire",None))
            or not callable(getattr(runtime_pool,"acquire",None))
            or not callable(getattr(public_get,"get",None))
        ):
            raise V40OwnedBarsHalted("V40_OWNED_BAR_READER_DEPENDENCIES_INVALID")
        self.scanner_pool=scanner_pool
        self.runtime_pool=runtime_pool
        self.public_get=public_get

    @staticmethod
    async def _identity(conn:Any,*,role:str)->None:
        if (
            await conn.fetchval("SELECT current_user")!=role
            or await conn.fetchval("SELECT current_database()")!=EXPECTED_DATABASE
            or await conn.fetchval("SELECT current_setting('transaction_read_only')")!="on"
            or await conn.fetchval("SELECT current_setting('transaction_isolation')")!="repeatable read"
        ):
            raise V40OwnedBarsHalted("V40_OWNED_BAR_SQL_READONLY_ROLE_MISMATCH")

    async def _read_cursors(self,ids:tuple[str,...])->tuple[V40BarCursorState,...]:
        async with self.runtime_pool.acquire() as conn:
            async with conn.transaction(isolation="repeatable_read",readonly=True):
                await self._identity(conn,role=TARGET_ROLE)
                bar_reader=V40DurableBarCursor(conn)
                return tuple([await bar_reader.read(pid) for pid in ids])

    async def _read_scanner_bars(
        self, due:Mapping[str,int],now_ts:int,
    )->dict[str,V40CompletedBar]:
        if not due:
            return {}
        async with self.scanner_pool.acquire() as conn:
            async with conn.transaction(isolation="repeatable_read",readonly=True):
                await self._identity(conn,role=RESTRICTED_V40_SIGNAL_READER_ROLE)
                bars={}
                for symbol,open_ts in due.items():
                    records=await conn.fetch(
                        """SELECT open_time,open,high,low,close
                           FROM public.candles
                           WHERE symbol=$1 AND interval='Min30'
                             AND open_time=to_timestamp($2)
                           LIMIT 2""",symbol,open_ts,
                    )
                    if type(records) not in (list,tuple) or len(records)!=1:
                        raise V40OwnedBarsHalted("V40_OWNED_BAR_MISSING_OR_DUPLICATE_CANDLE")
                    bars[symbol]=_bar_from_scanner_row(
                        dict(records[0]),expected_ts=open_ts,now_ts=now_ts,
                    )
                return bars

    async def _public_tick(self,symbol:str)->D:
        try:
            response=await self.public_get.get(
                "/api/v1/contract/detail",params={"symbol":symbol},
            )
            response.raise_for_status()
            payload=response.json()
        except Exception:
            raise V40OwnedBarsHalted("V40_OWNED_BAR_PUBLIC_TICK_GET_FAILED") from None
        if type(payload) is not dict or payload.get("success") is not True:
            raise V40OwnedBarsHalted("V40_OWNED_BAR_PUBLIC_TICK_API_REJECTED")
        return _spec_tick(payload.get("data"),symbol=symbol)

    async def observe(
        self, *,position_ids:Sequence[str],
        symbol_by_position:Mapping[str,str],
        now_ts:int,
    )->V40OwnedDueBarWindow:
        if (
            type(position_ids) not in (list,tuple)
            or not 1<=len(position_ids)<=MAX_SLOTS
            or any(type(pid) is not str or not pid.isascii()
                   or not pid.isdecimal() or pid.startswith("0")
                   for pid in position_ids)
            or len(set(position_ids))!=len(position_ids)
            or type(symbol_by_position) is not dict
            or set(symbol_by_position)!=set(position_ids)
            or any(not _symbol(s) for s in symbol_by_position.values())
            or len(set(symbol_by_position.values()))!=len(position_ids)
            or type(now_ts) is not int or now_ts<=0
        ):
            raise V40OwnedBarsHalted("V40_OWNED_BAR_INPUT_SHAPE_INVALID")
        # At least 8 s after the scheduled bar close before using scanner
        # OHLC; an unclosed or merely announced bar cannot be consumed.
        safe_open=((now_ts-MIN_CLOSE_FINALITY_SECONDS)//BAR_SECONDS-1)*BAR_SECONDS
        try:
            cursors=await self._read_cursors(tuple(position_ids))
            due={}
            no_due=[]
            for cursor in cursors:
                if cursor.position_id not in symbol_by_position:
                    raise V40OwnedBarsHalted("V40_OWNED_BAR_CURSOR_IDENTITY_MISMATCH")
                at=next_due_open_ts(cursor)
                if at<=safe_open:
                    due[symbol_by_position[cursor.position_id]]=at
                else:
                    no_due.append(cursor.position_id)
            bars=await self._read_scanner_bars(due,now_ts)
            ticks={}
            for symbol in due:
                ticks[symbol]=await self._public_tick(symbol)
            return V40OwnedDueBarWindow(
                "V40_OWNED_MIN30_DUE_CANDLES_VERIFIED_NONAUTHORIZING",
                cursors=cursors,bars=bars,ticks=ticks,
                no_due_position_ids=tuple(no_due),
                latest_safe_open_ts=safe_open,
                scanner_role_verified=bool(due),
                runtime_role_verified=True,
                exchange_public_specs_observed=len(ticks)==len(due),
            )
        except V40OwnedBarsHalted:
            raise
        except Exception:
            raise V40OwnedBarsHalted("V40_OWNED_BAR_READ_OR_VALIDATION_FAILED") from None
