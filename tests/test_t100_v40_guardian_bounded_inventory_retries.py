"""V40 signed guardian GET: two retries; emit a halt only after exhaustion.

No network or orders. All 3 attempts are freshly signed and pass only if a
genuine successful signed response is validated. A bad response or mismatch
is never an acceptable substitute for a complete account bookend.
"""
from __future__ import annotations
import httpx
import pytest
import app.t100_live_guardian_readonly_once as m
from app.t100_live_guardian_readonly_once import GuardianOnceHalted,GetOnlyMexcTransport
from tests.test_t100_v40_guardian_transient_positions_retry import FakeSequence

PATHS=(
    "/api/v1/private/position/open_positions",
    "/api/v1/private/stoporder/open_orders",
    "/api/v1/private/order/open_order_total_count",
    "/api/v1/private/account/assets",
)


def transport(events,retries=2):
    fake=FakeSequence(events)
    reader=GetOnlyMexcTransport(
        fake,api_key="a"*16,api_secret="b"*32,
        diagnostic_get_errors=True,transient_inventory_get_retries=retries,
    )
    return reader,fake


@pytest.mark.asyncio
@pytest.mark.parametrize("path",PATHS)
@pytest.mark.parametrize("first,second,delays",[
    (503,503,[1.0,2.0]),
    (429,429,[3.0,6.0]),
    (503,429,[1.0,6.0]),
    (httpx.ReadTimeout("SECRET",request=httpx.Request(
        "GET","https://api.mexc.com/api/v1/private/stoporder/open_orders")),
     503,[1.0,2.0]),
])
async def test_only_after_two_transient_retries_can_get_succeed(
    monkeypatch,capsys,path,first,second,delays,
):
    reader,fake=transport([first,second,200])
    pauses=[]
    async def pause(t):pauses.append(t)
    monkeypatch.setattr(m.asyncio,"sleep",pause)
    times=iter([1000,1002,1004])
    monkeypatch.setattr(m.time,"time",lambda:next(times))
    assert await reader._private_get(path)==[]
    assert len(fake.calls)==3
    assert pauses==delays
    headers=[a[1]["headers"] for a in fake.calls]
    assert [a["Request-Time"] for a in headers]==[
        "1000000","1002000","1004000",
    ]
    assert len(set(a["Signature"] for a in headers))==3
    assert capsys.readouterr().out==""


@pytest.mark.asyncio
@pytest.mark.parametrize("path",PATHS)
async def test_after_exactly_two_retries_fail_closed_with_original_safe_code(
    monkeypatch,capsys,path,
):
    reader,fake=transport([503,503,503,200])
    paused=[]
    async def pause(t):paused.append(t)
    monkeypatch.setattr(m.asyncio,"sleep",pause)
    with pytest.raises(GuardianOnceHalted,match="^GUARDIAN_GET_HTTP_5XX$"):
        await reader._private_get(path)
    assert len(fake.calls)==3
    assert paused==[1.0,2.0]
    assert capsys.readouterr().out==""


@pytest.mark.asyncio
@pytest.mark.parametrize("bad",[
    401,403,404,
    {"success":False,"code":602,"data":None},
    {"success":False,"code":510,"data":None},
])
async def test_never_retry_authorization_and_api_rejection(monkeypatch,bad):
    reader,fake=transport([bad,200])
    async def no_sleep(_):raise AssertionError("UNSAFE_RETRY")
    monkeypatch.setattr(m.asyncio,"sleep",no_sleep)
    with pytest.raises(GuardianOnceHalted):
        await reader._private_get(PATHS[1])
    assert len(fake.calls)==1


@pytest.mark.asyncio
async def test_noninventory_get_never_retries(monkeypatch):
    reader,fake=transport([503,200])
    async def no_sleep(_):raise AssertionError("UNSAFE_MODE_RETRY")
    monkeypatch.setattr(m.asyncio,"sleep",no_sleep)
    with pytest.raises(GuardianOnceHalted):
        await reader._private_get("/api/v1/private/position/position_mode")
    assert len(fake.calls)==1


def test_retry_count_is_bounded_typed_and_does_not_authorize_orders():
    for invalid in (True,False,-1,3,"2",2.5,None):
        with pytest.raises(GuardianOnceHalted,match="SIGNED_GET_INVENTORY_RETRY_COUNT_INVALID"):
            GetOnlyMexcTransport(
                FakeSequence([]),api_key="a"*16,api_secret="b"*32,
                transient_inventory_get_retries=invalid,
            )
    reader,_=transport([])
    assert not hasattr(reader,"_private_post")
