"""Read-only open-stops MEXC GET second chance; no authorization or POST.

Test with fake HTTP responses only. Each attempt must have a new signature.
"""
from __future__ import annotations
import asyncio
import httpx
import pytest

import app.t100_live_guardian_readonly_once as m
from app.t100_live_guardian_readonly_once import GetOnlyMexcTransport,GuardianOnceHalted
from tests.test_t100_v40_guardian_transient_positions_retry import FakeSequence

_STOPS="/api/v1/private/stoporder/open_orders"
_POS="/api/v1/private/position/open_positions"
_OTHER="/api/v1/private/account/assets"
_MODE="/api/v1/private/position/position_mode"
_REQ=httpx.Request("GET","https://api.mexc.com"+_STOPS)


def new_client(events,*,retry_stops=False,retry_positions=False):
    client=FakeSequence(events)
    reader=GetOnlyMexcTransport(
        client,api_key="a"*16,api_secret="b"*32,
        diagnostic_get_errors=True,
        retry_transient_positions_get=retry_positions,
        retry_transient_stops_get=retry_stops,
    )
    return reader,client


@pytest.mark.asyncio
async def test_default_stops_5xx_still_fails_closed_with_no_retry(monkeypatch):
    reader,client=new_client([503,200])
    async def unexpected(_):raise AssertionError("NO_SECOND_CHANCE")
    monkeypatch.setattr(m.asyncio,"sleep",unexpected)
    with pytest.raises(GuardianOnceHalted,match="^GUARDIAN_GET_HTTP_5XX$"):
        await reader._private_get(_STOPS)
    assert len(client.calls)==1


@pytest.mark.asyncio
@pytest.mark.parametrize("fail,seconds",[
    (500,1.0),(502,1.0),(503,1.0),(429,3.0),
    (httpx.ReadTimeout("secret",request=_REQ),1.0),
    (httpx.ConnectError("secret",request=_REQ),1.0),
])
async def test_stops_transient_retry_uses_fresh_signatures(monkeypatch,fail,seconds):
    reader,client=new_client([fail,200],retry_stops=True)
    sleeps=[]
    async def pause(d):sleeps.append(d)
    monkeypatch.setattr(m.asyncio,"sleep",pause)
    clock=iter([1234.0,1236.0])
    monkeypatch.setattr(m.time,"time",lambda:next(clock))
    assert await reader._private_get(_STOPS)==[]
    assert len(client.calls)==2
    assert sleeps==[seconds]
    assert all(path==_STOPS for path,_ in client.calls)
    assert client.calls[0][1]["headers"]["Request-Time"]=="1234000"
    assert client.calls[1][1]["headers"]["Request-Time"]=="1236000"
    assert client.calls[0][1]["headers"]["Signature"]!=client.calls[1][1]["headers"]["Signature"]


@pytest.mark.asyncio
async def test_second_stops_5xx_is_critical_and_not_retried_third_time(monkeypatch):
    reader,client=new_client([503,503,200],retry_stops=True)
    pauses=[]
    async def pause(d):pauses.append(d)
    monkeypatch.setattr(m.asyncio,"sleep",pause)
    with pytest.raises(GuardianOnceHalted,match="^GUARDIAN_GET_HTTP_5XX$"):
        await reader._private_get(_STOPS)
    assert len(client.calls)==2 and pauses==[1.0]


@pytest.mark.asyncio
@pytest.mark.parametrize("bad",[
    401,403,404,
    {"success":False,"code":602,"data":None},
    {"success":False,"code":510,"data":None},
    {"success":True,"code":0,"data":"BAD_SCHEMA"},
])
async def test_stops_auth_api_and_schema_rejection_do_not_retry(monkeypatch,bad):
    reader,client=new_client([bad,200],retry_stops=True)
    async def unexpected(_):raise AssertionError("UNSAFE_RETRY")
    monkeypatch.setattr(m.asyncio,"sleep",unexpected)
    if type(bad) is dict and bad.get("data")=="BAD_SCHEMA":
        assert await reader._private_get(_STOPS)=="BAD_SCHEMA"
    else:
        with pytest.raises(GuardianOnceHalted):
            await reader._private_get(_STOPS)
    assert len(client.calls)==1


@pytest.mark.asyncio
@pytest.mark.parametrize("path",[_POS,_OTHER,_MODE])
async def test_stops_retry_does_not_extend_to_other_endpoints(monkeypatch,path):
    reader,client=new_client([503,200],retry_stops=True)
    async def unexpected(_):raise AssertionError("WRONG_ENDPOINT_RETRIED")
    monkeypatch.setattr(m.asyncio,"sleep",unexpected)
    with pytest.raises(GuardianOnceHalted):
        await reader._private_get(path)
    assert len(client.calls)==1


def test_stop_retry_is_typed_and_read_only():
    with pytest.raises(GuardianOnceHalted,match="STOPS_RETRY_POLICY_INVALID"):
        GetOnlyMexcTransport(FakeSequence([]),api_key="a"*16,
                             api_secret="b"*32,diagnostic_get_errors=True,
                             retry_transient_stops_get="true")
    reader,_=new_client([],retry_stops=True)
    assert not hasattr(reader,"_private_post")
