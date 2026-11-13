"""Realized UTC-day loss from complete, signed MEXC history (GET only).

The dedicated first V40 canary must not pretend today's P&L is zero. Read
*all* bounded historical closed positions rather than filtering by opening
time (a position opened yesterday may close today). An incomplete, truncated,
moving or malformed history fails closed. Loss counts negative realized P&L
plus full reported fees/funding conservatively (possibly double-counted).
No trade, mutation, order permission, or cross-margin certificate is granted.
"""
from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal as D, InvalidOperation
from typing import Any, Awaitable, Callable, Protocol

from app.t100_live_guardian_readonly_once import GetOnlyMexcTransport
from app.t100_v40_executor_assembly import V40FirstPilotGetOnly

PATH="/api/v1/private/position/list/history_positions"
SIZE=100
MAX_PAGES=4


class V40DailyLossHalted(RuntimeError):
    pass


class V40HistoryReader(Protocol):
    async def page(self, number:int)->Any: ...


class V40HistoryGetOnly(V40FirstPilotGetOnly):
    """One fixed extra GET, strict pagination, no POST transport."""
    @classmethod
    def _allowed(cls,path:str)->bool:
        return path==PATH or super()._allowed(path)

    async def _private_get(self,path:str,params:dict[str,Any]|None=None)->Any:
        if path==PATH:
            if (
                type(params) is not dict
                or set(params)!={"page_num","page_size"}
                or type(params["page_num"]) is not int
                or not 1<=params["page_num"]<=MAX_PAGES
                or params["page_size"]!=SIZE
            ):
                raise V40DailyLossHalted("V40_DAILY_LOSS_HISTORY_QUERY_OUT_OF_SCOPE")
            # The base class owns the signed GET and allowlist, with no
            # mutating method or authority to change any account setting.
            return await GetOnlyMexcTransport._private_get(self,path,params)
        return await super()._private_get(path,params)

    async def page(self,number:int)->Any:
        if type(number) is not int or not 1<=number<=MAX_PAGES:
            raise V40DailyLossHalted("V40_DAILY_LOSS_PAGE_INVALID")
        return await self._private_get(
            PATH,{"page_num":number,"page_size":SIZE},
        )


@dataclass(frozen=True)
class V40DailyLossEvidence:
    status:str
    realized_today_loss_usdt:str|None=None
    closed_positions_today:int=0
    total_historical_positions:int=0
    complete_two_passes:bool=False
    complete_history_verified:bool=False
    safely_below_12_50:bool=False
    read_only:bool=True
    database_writes:int=0
    mexc_posts:int=0
    entry_allowed:bool=False
    live_authorization:bool=False


def _money(value:Any)->D:
    if type(value) is bool or type(value) not in (str,int,float,D):
        raise V40DailyLossHalted("V40_DAILY_LOSS_NUMERIC_FIELD_INVALID")
    try:
        number=D(str(value))
    except (InvalidOperation,ValueError,TypeError):
        raise V40DailyLossHalted("V40_DAILY_LOSS_NUMERIC_FIELD_INVALID") from None
    if not number.is_finite():
        raise V40DailyLossHalted("V40_DAILY_LOSS_NUMERIC_FIELD_INVALID")
    return number


def _rows(raw:Any,page:int)->tuple[list[dict[str,Any]],int|None,int|None]:
    if type(raw) is list:
        rows=raw
        declared_total=declared_pages=None
    elif type(raw) is dict:
        rows=raw.get("resultList")
        declared_total=raw.get("totalCount")
        declared_pages=raw.get("totalPage")
        if (
            type(raw.get("currentPage")) is not int
            or raw["currentPage"]!=page
            or type(raw.get("pageSize")) is not int
            or raw["pageSize"]!=SIZE
            or type(declared_total) is not int
            or declared_total<0
            or type(declared_pages) is not int
            or not 1<=declared_pages<=MAX_PAGES
        ):
            raise V40DailyLossHalted("V40_DAILY_LOSS_HISTORY_PAGINATION_INVALID")
    else:
        raise V40DailyLossHalted("V40_DAILY_LOSS_HISTORY_SHAPE_INVALID")
    if type(rows) is not list or len(rows)>SIZE or any(
        type(row) is not dict for row in rows
    ):
        raise V40DailyLossHalted("V40_DAILY_LOSS_HISTORY_ROWS_INVALID")
    return rows,declared_total,declared_pages


async def _all_rows(reader:V40HistoryReader)->tuple[tuple[str,...],list[dict[str,Any]]]:
    import json
    results=[]
    declared_total=declared_pages=None
    complete=False
    for page in range(1,MAX_PAGES+1):
        raw=await reader.page(page)
        rows,total,pages=_rows(raw,page)
        if total is not None:
            if declared_total is None:
                declared_total,declared_pages=total,pages
            elif (total,pages)!=(declared_total,declared_pages):
                raise V40DailyLossHalted("V40_DAILY_LOSS_HISTORY_PAGINATION_DRIFT")
        elif declared_total is not None:
            raise V40DailyLossHalted("V40_DAILY_LOSS_HISTORY_PAGINATION_DRIFT")
        results.extend(rows)
        if declared_pages is not None and page==declared_pages:
            complete=len(results)==declared_total
            break
        if declared_pages is None and len(rows)<SIZE:
            complete=True
            break
        if declared_pages is not None and (
            page>=declared_pages or len(results)>declared_total
        ):
            raise V40DailyLossHalted("V40_DAILY_LOSS_HISTORY_COUNT_CONFLICT")
    if not complete:
        raise V40DailyLossHalted("V40_DAILY_LOSS_HISTORY_TRUNCATED")
    # Exact account identity/order history across two independent reads.
    seen=set()
    encoded=[]
    for row in results:
        identifier=row.get("positionId")
        if (
            type(identifier) not in (int,str)
            or not str(identifier).isascii()
            or not str(identifier).isdigit()
            or int(identifier)<=0 or str(identifier) in seen
            or row.get("state")!=3
            or type(row.get("updateTime")) is not int
            or not 0<row["updateTime"]<4102444800000
        ):
            raise V40DailyLossHalted("V40_DAILY_LOSS_HISTORY_IDENTITY_INVALID")
        seen.add(str(identifier))
        for key in ("realised","totalFee","holdFee"):
            _money(row.get(key))
        # Preserve complete rows for strict drift comparison even when extra
        # venue fields move (fail closed, never assume a harmless change).
        try:
            encoded.append(json.dumps(
                row,sort_keys=True,separators=(",",":"),allow_nan=False,
            ))
        except (TypeError,ValueError):
            raise V40DailyLossHalted("V40_DAILY_LOSS_HISTORY_ENCODING_INVALID") from None
    return tuple(sorted(encoded)),results


async def observe_daily_loss(
    reader:V40HistoryReader,*,now:datetime,
    pause:Callable[[float],Awaitable[Any]]=asyncio.sleep,
)->V40DailyLossEvidence:
    fail=lambda code:V40DailyLossEvidence(code)
    if (
        type(now) is not datetime or now.tzinfo is None
        or now.utcoffset() is None
        or not callable(pause)
    ):
        return fail("V40_DAILY_LOSS_CLOCK_OR_READER_INVALID")
    utc=now.astimezone(timezone.utc)
    midnight=utc.replace(hour=0,minute=0,second=0,microsecond=0)
    cutoff=int(midnight.timestamp()*1000)
    end=int(now.timestamp()*1000)
    try:
        first,records=await _all_rows(reader)
        await pause(1.)
        second,again=await _all_rows(reader)
        if first!=second or len(records)!=len(again):
            return fail("V40_DAILY_LOSS_HISTORY_CHANGED_BETWEEN_PASSES")
        loss=D(0)
        today=0
        for row in again:
            ts=row["updateTime"]
            if ts>end+1000:
                return fail("V40_DAILY_LOSS_FUTURE_HISTORY_TIMESTAMP")
            if ts<cutoff:
                continue
            today+=1
            realised=_money(row["realised"])
            fee=_money(row["totalFee"])
            funding=_money(row["holdFee"])
            # Conservatively count all reported fees and paid funding on top
            # of negative realized P&L, even if realised already includes them.
            loss+=max(D(0),-realised)+abs(fee)+max(D(0),-funding)
        return V40DailyLossEvidence(
            "V40_DAILY_LOSS_TWO_COMPLETE_SIGNED_GET_PASSES_NONAUTHORIZING",
            realized_today_loss_usdt=str(loss),
            closed_positions_today=today,
            total_historical_positions=len(again),
            complete_two_passes=True,
            complete_history_verified=True,
            safely_below_12_50=loss<D("12.50"),
        )
    except V40DailyLossHalted as exc:
        return fail(str(exc))
    except Exception:
        return fail("V40_DAILY_LOSS_SIGNED_HISTORY_UNAVAILABLE")
