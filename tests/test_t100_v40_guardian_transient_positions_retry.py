"""Guard one transient MEXC open-positions GET without weakening V40 safety.

Offline-only: all requests are fake GETs. The source is not a trader, a latch
reset, or an account-completeness certificate.
"""
from __future__ import annotations

import asyncio
import httpx
import pytest

import app.t100_live_guardian_readonly_once as m
from app.t100_live_guardian_readonly_once import GetOnlyMexcTransport, GuardianOnceHalted

_POS="/api/v1/private/position/open_positions"
_STOPS="/api/v1/private/stoporder/open_orders"
_REQUEST=httpx.Request("GET","https://api.mexc.com"+_POS)


class FakeSequence:
    def __init__(self,events):
        self.events=list(events)
        self.calls=[]
    async def get(self,path,**kwargs):
        self.calls.append((path,kwargs))
        event=self.events.pop(0)
        if isinstance(event,Exception):
            raise event
        if isinstance(event,int):
            return httpx.Response(
                event,request=httpx.Request("GET","https://api.mexc.com"+path),
                json={"success":event==200,"code":0,"data":[]},
            )
        return httpx.Response(
            200,request=httpx.Request("GET","https://api.mexc.com"+path),
            json=event,
        )


def client(events,*,retry=False,diagnostics=True):
    c=FakeSequence(events)
    t=GetOnlyMexcTransport(
        c,api_key="a"*16,api_secret="b"*32,
        diagnostic_get_errors=diagnostics,
        retry_transient_positions_get=retry,
    )
    return t,c


@pytest.mark.asyncio
async def test_default_transient_positions_get_still_halts_without_retry(monkeypatch):
    t,c=client([httpx.ReadTimeout("secret",request=_REQUEST),200])
    async def no_sleep(_):
        raise AssertionError("No retry should occur")
    monkeypatch.setattr(m.asyncio,"sleep",no_sleep)
    with pytest.raises(GuardianOnceHalted,match="^GUARDIAN_GET_TIMEOUT$"):
        await t._private_get(_POS)
    assert len(c.calls)==1


@pytest.mark.asyncio
@pytest.mark.parametrize("failure,delay",[
    (httpx.ReadTimeout("secret",request=_REQUEST),1.0),
    (httpx.ConnectError("secret",request=_REQUEST),1.0),
    (503,1.0),
    (429,3.0),
])
async def test_one_opt_in_retry_requires_second_fresh_signed_get(monkeypatch,failure,delay):
    t,c=client([failure,200],retry=True)
    pauses=[]
    async def sleep(d):pauses.append(d)
    monkeypatch.setattr(m.asyncio,"sleep",sleep)
    ticks=iter([1000,1002])
    monkeypatch.setattr(m.time,"time",lambda:next(ticks))
    assert await t._private_get(_POS)==[]
    assert len(c.calls)==2
    assert pauses==[delay]
    assert all(path==_POS for path,kwargs in c.calls)
    assert c.calls[0][1]["headers"]["Request-Time"]=="1000000"
    assert c.calls[1][1]["headers"]["Request-Time"]=="1002000"
    assert c.calls[0][1]["headers"]["Signature"]!=c.calls[1][1]["headers"]["Signature"]


@pytest.mark.asyncio
@pytest.mark.parametrize("failure",[
    401,403,418,
    {"success":False,"code":510,"data":None,"msg":"hidden"},
    {"success":False,"code":602,"data":None},
    {"success":True,"code":0,"data":"BAD_FORMAT"},
])
async def test_permission_api_and_schema_failures_never_get_retried(monkeypatch,failure):
    t,c=client([failure,200],retry=True)
    async def no_sleep(_):raise AssertionError("UNSAFE_RETRY")
    monkeypatch.setattr(m.asyncio,"sleep",no_sleep)
    if isinstance(failure,dict) and failure.get("data")=="BAD_FORMAT":
        assert await t._private_get(_POS)=="BAD_FORMAT"
    else:
        with pytest.raises(GuardianOnceHalted):
            await t._private_get(_POS)
    assert len(c.calls)==1


@pytest.mark.asyncio
async def test_second_timeout_fails_closed_without_third_attempt(monkeypatch):
    t,c=client([
        httpx.ReadTimeout("secret",request=_REQUEST),
        httpx.ReadTimeout("secret2",request=_REQUEST),
        200,
    ],retry=True)
    paused=[]
    async def sleep(d):paused.append(d)
    monkeypatch.setattr(m.asyncio,"sleep",sleep)
    with pytest.raises(GuardianOnceHalted,match="^GUARDIAN_GET_TIMEOUT$"):
        await t._private_get(_POS)
    assert len(c.calls)==2
    assert paused==[1.0]


@pytest.mark.asyncio
async def test_opt_in_never_retries_other_inventory_or_mode_endpoints(monkeypatch):
    t,c=client([503,200],retry=True)
    async def no_sleep(_):raise AssertionError("UNSAFE_RETRY")
    monkeypatch.setattr(m.asyncio,"sleep",no_sleep)
    with pytest.raises(GuardianOnceHalted,match="^GUARDIAN_GET_HTTP_5XX$"):
        await t._private_get(_STOPS)
    assert len(c.calls)==1


def test_retry_must_be_explicit_boolean_and_does_not_authorize_trade():
    with pytest.raises(GuardianOnceHalted,match="POSITIONS_RETRY_POLICY_INVALID"):
        GetOnlyMexcTransport(
            FakeSequence([]),api_key="a"*16,api_secret="b"*32,
            diagnostic_get_errors=True,retry_transient_positions_get="true",
        )
    t,_=client([],retry=True)
    assert not hasattr(t,"_private_post")
