"""Offline tests for exact live V40 Min30 cursor-to-scanner reader.

No private HTTP, MEXC POST, DB modification or account credentials.
"""
from __future__ import annotations

from dataclasses import replace
from datetime import datetime,timezone
from decimal import Decimal as D
import pytest

from app import t100_v40_owned_completed_bar_reader as m
from app.t100_live_db_inspect import EXPECTED_DATABASE
from app.t100_live_role_preflight import TARGET_ROLE
from app.t100_live_v40_stage2_source import RESTRICTED_V40_SIGNAL_READER_ROLE

OPEN=1_800_000_000  # Exactly aligned Min30 open.
NOW=OPEN+1808
PID="52"
SYMBOL="SIREN_USDT"


class AsyncContext:
    def __init__(self,thing):self.thing=thing
    async def __aenter__(self):return self.thing
    async def __aexit__(self,*_):return False


class Conn:
    def __init__(self,role,*,source=False):
        self.role=role
        self.source=source
        self.calls=[]
        self.candle={
            "open_time":datetime.fromtimestamp(OPEN,timezone.utc),
            "open":"100","high":"105","low":"96","close":"101",
        }
        self.candle_count=1
        self.last_committed=None
        self.pending=None
        self.full_cursor={
            "position_id":PID,
            "entry_external_oid":"t100v1efoo",
            "signed_fill_at_ms":(OPEN-1200)*1000,
            "last_committed_open_ts":None,
            "pending_open_ts":None,
            "pending_status":None,
            "pending_stop_price":None,
            "pending_best_profit_pct":None,
            "pending_trail_armed":None,
            "checkpoint_version":1,
            "stop_order_id":"92",
            "confirmed_stop_price":D("175"),
            "best_profit_pct":D("0"),
            "trail_armed":False,
            "cursor_revision":1,
        }
    def transaction(self,**kw):
        assert kw=={"isolation":"repeatable_read","readonly":True}
        return AsyncContext(self)
    async def fetchval(self,sql):
        self.calls.append(("fetchval",sql))
        if sql=="SELECT current_user":return self.role
        if sql=="SELECT current_database()":return EXPECTED_DATABASE
        if sql=="SELECT current_setting('transaction_read_only')":return "on"
        if sql=="SELECT current_setting('transaction_isolation')":return "repeatable read"
        raise AssertionError("Unexpected readonly role SQL")
    async def fetchrow(self,sql,*args):
        self.calls.append(("fetchrow",sql))
        assert not self.source
        assert "FROM public.t100_v40_exit_bar_cursor" in sql
        assert args==(PID,)
        return dict(self.full_cursor)
    async def fetch(self,sql,*args):
        self.calls.append(("fetch",sql))
        assert self.source
        assert "FROM public.candles" in sql
        assert "LIMIT 2" in sql
        assert args==(SYMBOL,OPEN)
        return [dict(self.candle) for _ in range(self.candle_count)]


class Pool:
    def __init__(self,conn): self.conn=conn
    def acquire(self):return AsyncContext(self.conn)


class Response:
    def __init__(self,symbol=SYMBOL,tick="0.001",allowed=True):
        self.symbol=symbol
        self.tick=tick
        self.allowed=allowed
    def raise_for_status(self):pass
    def json(self):
        return {"success":True,"data":[{
            "symbol":self.symbol,"apiAllowed":self.allowed,
            "priceUnit":self.tick,
        }]}


class Public:
    def __init__(self):
        self.calls=[]
        self.response=Response()
    async def get(self,path,params):
        self.calls.append((path,params))
        return self.response


def make():
    source=Conn(RESTRICTED_V40_SIGNAL_READER_ROLE,source=True)
    runtime=Conn(TARGET_ROLE)
    pub=Public()
    obj=m.PostgresV40OwnedBarReader(
        scanner_pool=Pool(source),runtime_pool=Pool(runtime),
        public_get=pub,
    )
    return obj,source,runtime,pub


async def observe(reader,*,now=NOW):
    return await reader.observe(
        position_ids=(PID,),symbol_by_position={PID:SYMBOL},now_ts=now,
    )


def test_ceiling_fill_to_exact_post_fill_bar_boundary():
    r=m.V40BarCursorState(
        position_id=PID,entry_external_oid="origin",signed_fill_at_ms=(OPEN-1200)*1000,
        last_committed_open_ts=None,pending_open_ts=None,pending_status=None,
        pending_stop_price=None,pending_best_profit_pct=None,pending_trail_armed=None,
        checkpoint_version=1,stop_order_id="92",confirmed_stop_price=D("175"),
        best_profit_pct=D("0"),trail_armed=False,cursor_revision=1,
    )
    assert m.next_due_open_ts(r)==OPEN
    assert m.next_due_open_ts(replace(r,last_committed_open_ts=OPEN))==OPEN+1800
    with pytest.raises(m.V40OwnedBarsHalted,match="CHRONOLOGY"):
        m.next_due_open_ts(replace(r,last_committed_open_ts=OPEN-1800))


@pytest.mark.asyncio
async def test_exact_completed_bar_and_current_contract_tick_are_read_only():
    reader,source,runtime,pub=make()
    window=await observe(reader)
    assert window.status=="V40_OWNED_MIN30_DUE_CANDLES_VERIFIED_NONAUTHORIZING"
    assert window.no_due_position_ids==()
    assert list(window.bars)==[SYMBOL]
    assert window.bars[SYMBOL].open_ts==OPEN
    assert window.ticks=={SYMBOL:D("0.001")}
    assert window.scanner_role_verified and window.runtime_role_verified
    assert window.live_authorization is False and window.next_entry_allowed is False
    assert window.mexc_posts==0 and window.database_writes==0
    assert len(pub.calls)==1
    assert all("UPDATE" not in sql and "INSERT" not in sql
               for conn in (source,runtime) for _,sql in conn.calls)


@pytest.mark.asyncio
async def test_not_yet_completed_bar_must_not_be_fetched_or_traded():
    reader,source,runtime,pub=make()
    window=await observe(reader,now=OPEN+1807)
    assert window.no_due_position_ids==(PID,)
    assert window.bars==window.ticks=={}
    assert window.scanner_role_verified is False
    assert not pub.calls
    assert not source.calls


@pytest.mark.asyncio
@pytest.mark.parametrize("count",[0,2])
async def test_missing_or_ambiguous_scanner_bar_halts_entire_batch(count):
    reader,source,runtime,pub=make()
    source.candle_count=count
    with pytest.raises(m.V40OwnedBarsHalted,match="MISSING_OR_DUPLICATE"):
        await observe(reader)
    assert pub.calls==[]


@pytest.mark.asyncio
async def test_scanner_candle_wrong_timestamp_or_invalid_ohlc_halts():
    reader,source,runtime,pub=make()
    source.candle["open_time"]=datetime.fromtimestamp(OPEN+1800,timezone.utc)
    with pytest.raises(m.V40OwnedBarsHalted,match="EXACT_OPEN_TIMESTAMP"):
        await observe(reader)
    source.candle["open_time"]=datetime.fromtimestamp(OPEN,timezone.utc)
    source.candle["low"]="120"
    with pytest.raises(m.V40OwnedBarsHalted,match="SCANNER_OHLC"):
        await observe(reader)
    assert not pub.calls


@pytest.mark.asyncio
async def test_active_pending_cursor_cannot_be_skipped(monkeypatch):
    reader,source,runtime,pub=make()
    runtime.full_cursor.update(
        pending_open_ts=OPEN,pending_status="KEEP_VERIFIED_NATIVE_STOP",
        pending_stop_price=D("175"),pending_best_profit_pct=D("0"),
        pending_trail_armed=False,
    )
    with pytest.raises(m.V40OwnedBarsHalted,match="CURSOR_PENDING"):
        await observe(reader)
    assert not source.calls and not pub.calls


@pytest.mark.asyncio
async def test_scanner_sql_role_or_readonly_mode_must_be_exact():
    reader,source,runtime,pub=make()
    source.role="mexc_scanner"
    with pytest.raises(m.V40OwnedBarsHalted,match="ROLE_MISMATCH"):
        await observe(reader)
    assert not pub.calls
    reader,source,runtime,pub=make()
    runtime.role="mexc_scanner"
    with pytest.raises(m.V40OwnedBarsHalted,match="ROLE_MISMATCH"):
        await observe(reader)
    assert not pub.calls


@pytest.mark.asyncio
@pytest.mark.parametrize("allowed,tick",[
    (False,"0.001"),(True,"0"),(True,"NaN"),(True,"abc"),
])
async def test_contract_not_api_allowed_or_bad_tick_blocks_projection(allowed,tick):
    reader,source,runtime,pub=make()
    pub.response=Response(allowed=allowed,tick=tick)
    with pytest.raises(m.V40OwnedBarsHalted):
        await observe(reader)


@pytest.mark.asyncio
async def test_no_unowned_positions_unsupported_symbols_or_bad_clock():
    reader,source,runtime,pub=make()
    for positions,symbols,now in [
        ((),{},NOW),
        ((PID,),{PID:"BTC_USDT"},NOW),
        ((PID,PID),{PID:SYMBOL},NOW),
        ((PID,),{PID:SYMBOL},False),
    ]:
        with pytest.raises(m.V40OwnedBarsHalted,match="INPUT_SHAPE"):
            await reader.observe(
                position_ids=positions,symbol_by_position=symbols,now_ts=now,
            )
    assert not source.calls and not pub.calls
