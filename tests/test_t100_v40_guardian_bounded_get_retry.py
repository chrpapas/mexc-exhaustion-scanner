"""Opt-in single idempotent GET retry does not waive hedge/account failures."""
from __future__ import annotations

import httpx
import pytest

from app.t100_live_guardian_readonly_once import GetOnlyMexcTransport
from app.t100_live_readonly_probe import VenueReadHalted
from app.t100_v40_guardian_account_and_owned import _hedge_mode_bounded_get

_MODE="/api/v1/private/position/position_mode"
_REQ=httpx.Request("GET","https://api.mexc.com"+_MODE)


class SequenceClient:
    def __init__(self,statuses):
        self.statuses=list(statuses)
        self.calls=0
    async def get(self,path,**kwargs):
        assert path==_MODE
        assert kwargs["headers"].get("ApiKey")
        self.calls+=1
        event=self.statuses.pop(0)
        if isinstance(event,Exception):
            raise event
        if type(event) is int:
            return httpx.Response(
                event,json={"success":event==200,"code":0,"data":1},
                request=_REQ,
            )
        return httpx.Response(
            200,json=event,request=_REQ,
        )


def make(events):
    client=SequenceClient(events)
    return GetOnlyMexcTransport(
        client,api_key="a"*16,api_secret="b"*32,
    ),client


@pytest.mark.asyncio
async def test_default_production_policy_remains_sticky_on_one_failed_get():
    get,client=make([429,200])
    waits=[]
    with pytest.raises(VenueReadHalted,match="POSITION_MODE_HTTP_429"):
        await _hedge_mode_bounded_get(get,pause=lambda x:waits.append(x))
    assert client.calls==1 and waits==[]


@pytest.mark.asyncio
@pytest.mark.parametrize("failure,expected_delay",[
    (429,3.0),(503,1.0),
    (httpx.ReadTimeout("secret",request=_REQ),1.0),
    (httpx.ConnectError("secret",request=_REQ),1.0),
    ({"success":False,"code":510,"msg":"secret"},3.0),
    ({"success":False,"code":501,"msg":"secret"},1.0),
])
async def test_at_most_one_retry_if_safe_class_and_second_get_is_signed(
    failure,expected_delay,
):
    get,client=make([failure,200])
    waits=[]
    async def pause(seconds):waits.append(seconds)
    assert await _hedge_mode_bounded_get(
        get,retry_transient=True,pause=pause,
    )==1
    assert client.calls==2
    assert waits==[expected_delay]


@pytest.mark.asyncio
@pytest.mark.parametrize("failure",[
    401,403,418,
    {"success":False,"code":701,"msg":"token"},
    {"success":False,"code":602,"msg":"token"},
    {"success":False,"code":9999,"msg":"token"},
])
async def test_auth_permission_and_unknown_rejections_never_retry(failure):
    get,client=make([failure,200])
    waits=[]
    async def pause(seconds):waits.append(seconds)
    with pytest.raises(VenueReadHalted):
        await _hedge_mode_bounded_get(
            get,retry_transient=True,pause=pause,
        )
    assert client.calls==1 and waits==[]


@pytest.mark.asyncio
async def test_second_transient_failure_still_halts_without_third_attempt():
    get,client=make([429,503,200])
    waits=[]
    async def pause(seconds):waits.append(seconds)
    with pytest.raises(VenueReadHalted,match="POSITION_MODE_HTTP_5XX"):
        await _hedge_mode_bounded_get(
            get,retry_transient=True,pause=pause,
        )
    assert client.calls==2 and waits==[3.0]


@pytest.mark.asyncio
async def test_retry_does_not_trust_an_exchange_wrong_hedge_mode():
    get,client=make([429,{"success":True,"code":0,"data":0}])
    async def pause(_):pass
    # The helper returns the observed actual mode; the caller requires 1.
    mode=await _hedge_mode_bounded_get(
        get,retry_transient=True,pause=pause,
    )
    assert mode==0 and client.calls==2


@pytest.mark.asyncio
async def test_retry_policy_cannot_be_enabled_by_truthy_string():
    get,client=make([200])
    with pytest.raises(VenueReadHalted,match="RETRY_POLICY_INVALID"):
        await _hedge_mode_bounded_get(
            get,retry_transient="true",
        )
    assert client.calls==0
