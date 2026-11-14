"""Offline config/allowlist/durable journal tests for V40 standalone watcher."""
from __future__ import annotations

import asyncio
from dataclasses import replace
import hashlib
import hmac
from pathlib import Path
from unittest.mock import AsyncMock

import httpx
import pytest

import app.t100_v40_cross_watchdog_service as m


KEY="fake-dedicated-readonly-key"
SECRET="fake-test-secret-never-real"
HASH=hashlib.sha256(KEY.encode()).hexdigest()


def env(tmp_path):
    return {
        "T100_V40_WATCH_ENABLED":"true",
        "T100_EXECUTION_MODE":"paper",
        "T100_LIVE_ORDER_API_ENABLED":"false",
        "T100_LIVE_KILL_SWITCH":"true",
        "T100_V40_WATCH_KEYCHAIN_SERVICE":m.KEYCHAIN_READONLY,
        "T100_V40_WATCH_DISCORD_KEYCHAIN_SERVICE":m.KEYCHAIN_DISCORD,
        "T100_V40_WATCH_API_KEY_SHA256":HASH,
        "T100_V40_WATCH_MODE":"FLAT",
        "T100_V40_WATCH_JOURNAL":str(tmp_path/"flat-watch.sqlite"),
    }


def test_flat_watcher_config_pins_account_and_stays_paper(tmp_path):
    out=m.configure_v40_watch(env(tmp_path),api_key=KEY,cycles=1)
    assert out.scope.mode=="FLAT"
    assert out.scope.account_key_sha256==HASH
    assert out.max_cycles==1
    assert out.poll_seconds==1
    assert out.journal_path.is_absolute()
    assert not out.journal_path.exists()


@pytest.mark.parametrize("key,value",[
    ("T100_EXECUTION_MODE","live"),
    ("T100_LIVE_ORDER_API_ENABLED","true"),
    ("T100_LIVE_KILL_SWITCH","false"),
    ("T100_V40_WATCH_ENABLED","false"),
    ("T100_V40_WATCH_KEYCHAIN_SERVICE","boxwave-key"),
    ("T100_V40_WATCH_DISCORD_KEYCHAIN_SERVICE","prod-webhook"),
])
def test_bad_or_live_environment_refused(tmp_path,key,value):
    candidate={**env(tmp_path),key:value}
    with pytest.raises(m.V40WatchServiceHalted):
        m.configure_v40_watch(candidate,api_key=KEY,cycles=1)


def test_pinned_fingerprint_prevents_wrong_subaccount_key(tmp_path):
    with pytest.raises(m.V40WatchServiceHalted,match="PIN_REQUIRED"):
        m.configure_v40_watch(
            env(tmp_path),api_key="wrong-dedicated-key-123",cycles=1,
        )


def test_no_automatic_flat_to_owned_adoption(tmp_path):
    first=env(tmp_path)
    first["T100_V40_WATCH_SYMBOL"]="EXAMPLE_USDT"
    with pytest.raises(m.V40WatchServiceHalted,match="REVIEWED_SCOPE_REQUIRED"):
        m.configure_v40_watch(first,api_key=KEY,cycles=1)


def test_owned_scope_pins_entry_stop_quantity_and_has_distinct_journal_hash(tmp_path):
    flat=m.configure_v40_watch(env(tmp_path),api_key=KEY,cycles=1)
    e={**env(tmp_path),
       "T100_V40_WATCH_MODE":"OWNED",
       "T100_V40_WATCH_SYMBOL":"EXAMPLE_USDT",
       "T100_V40_WATCH_POSITION_ID":"900",
       "T100_V40_WATCH_ORIGIN_OID":"t100v40pilot_entry_12345",
       "T100_V40_WATCH_ENTRY_ORDER_ID":"123456",
       "T100_V40_WATCH_STOP_ID":"99",
       "T100_V40_WATCH_CONTRACTS":"250",
       "T100_V40_WATCH_MAX_STOP_PRICE":"3.5"}
    owned=m.configure_v40_watch(e,api_key=KEY,cycles=1)
    assert owned.scope.mode=="OWNED"
    assert owned.scope_sha256!=flat.scope_sha256
    e["T100_V40_WATCH_STOP_ID"]="100"
    replacement=m.configure_v40_watch(e,api_key=KEY,cycles=1)
    assert replacement.scope_sha256!=owned.scope_sha256
    e["T100_V40_WATCH_MAX_STOP_PRICE"]="NaN"
    with pytest.raises(m.V40WatchServiceHalted):
        m.configure_v40_watch(e,api_key=KEY,cycles=1)


def test_signed_get_transport_has_hard_allowlist_and_no_post():
    async def scenario():
        transport=m.V40WatchSignedGetOnly(KEY,SECRET)
        try:
            req=httpx.Request(
                "GET","https://api.mexc.com/api/v1/private/position/open_positions"
            )
            transport._http.get=AsyncMock(return_value=httpx.Response(
                200,json={"success":True,"code":0,"data":[]},request=req,
            ))
            result=await transport._private_get(
                "/api/v1/private/position/open_positions",
            )
            assert result==[]
            assert not hasattr(transport,"_private_post")
            headers=transport._http.get.call_args.kwargs["headers"]
            expected=hmac.new(
                SECRET.encode(),(KEY+headers["Request-Time"]).encode(),
                hashlib.sha256,
            ).hexdigest()
            assert headers["Signature"]==expected
            for bad in (
                "/api/v1/private/order/create",
                "/api/v1/private/order/cancel",
                "/api/v1/private/order/external/BTC_USDT/evil/slash",
                "/api/v1/private/order/deal_details/0",
            ):
                with pytest.raises(m.V40WatchServiceHalted,match="PATH_NOT_ALLOWED"):
                    await transport._private_get(bad)
            with pytest.raises(m.V40WatchServiceHalted,match="QUERY_NOT_ALLOWED"):
                await transport._private_get(
                    "/api/v1/private/position/open_positions",
                    {"symbol":"EXAMPLE_USDT"},
                )
            assert transport._http.get.await_count==1
        finally:
            await transport.close()
    asyncio.run(scenario())


def test_dedicated_keychain_scope_checked_before_any_reads(monkeypatch,tmp_path):
    calls=[]
    monkeypatch.setattr(m,"_from_keychain",lambda *a:(
        calls.append(a) or "NOT_USED"
    ))
    candidate={**env(tmp_path),"T100_V40_WATCH_KEYCHAIN_SERVICE":"BOXWAVE"}
    with pytest.raises(m.V40WatchServiceHalted,match="PAPER_KILL_AND_SCOPE_REQUIRED"):
        asyncio.run(m.run_v40_watch(environ=candidate,cycles=1))
    assert calls==[]
    candidate={**env(tmp_path),"T100_LIVE_KILL_SWITCH":"false"}
    with pytest.raises(m.V40WatchServiceHalted,match="PAPER_KILL_AND_SCOPE_REQUIRED"):
        asyncio.run(m.run_v40_watch(environ=candidate,cycles=1))
    assert calls==[]


def test_real_loop_is_mocked_read_only_and_journals_flat_evidence(monkeypatch,tmp_path):
    candidate=env(tmp_path)
    calls=[]
    def keychain(service,account):
        calls.append((service,account))
        if service==m.KEYCHAIN_READONLY:
            return KEY if account==m.KEY_ACCOUNT else SECRET
        assert service==m.KEYCHAIN_DISCORD
        return "https://discord.com/api/webhooks/12345/testtoken"
    monkeypatch.setattr(m,"_from_keychain",keychain)

    class FakeTransport:
        def __init__(self,*args,**kwargs):pass
        async def close(self):pass
        async def _private_get(self,path,params=None):
            calls.append((path,params))
            if path=="/api/v1/private/position/open_positions":return []
            if path=="/api/v1/private/stoporder/open_orders":return []
            if path=="/api/v1/private/position/position_mode":
                return {"positionMode":1}
            if path=="/api/v1/private/order/open_order_total_count":
                return {
                    "sumCount":0,"stopOrderCount":0,"limitOrderCount":0,
                    "planOrderCount":0,"trackOrderCount":0,
                }
            raise AssertionError("UNEXPECTED_GET")

    class FakeNotifier:
        def __init__(self,*a,**k):pass
        async def close(self):pass
        async def send(self,*a,**k):
            raise AssertionError("NO_ALERT_EXPECTED_WHEN_FLAT")

    monkeypatch.setattr(m,"V40WatchSignedGetOnly",FakeTransport)
    monkeypatch.setattr(m,"T100LiveOpsNotifier",FakeNotifier)
    outcome=asyncio.run(m.run_v40_watch(environ=candidate,cycles=1))
    assert outcome.status=="WATCH_FLAT_INVENTORY_OBSERVED_NONAUTHORIZING"
    assert outcome.alarm_latched is False
    assert outcome.live_authorization is False
    assert outcome.mexc_orders_sent is False
    assert Path(candidate["T100_V40_WATCH_JOURNAL"]).exists()
    assert sum(1 for c in calls if isinstance(c,tuple) and
               c[0]=="/api/v1/private/position/open_positions")==2
    assert len([x for x in calls if x==(m.KEYCHAIN_READONLY,m.KEY_ACCOUNT)])==1
    assert len([x for x in calls if x==(m.KEYCHAIN_DISCORD,m.DISCORD_ACCOUNT)])==1


def test_change_in_account_scope_cannot_rearm_durable_watchdog_journal(
    monkeypatch,tmp_path,
):
    candidate=env(tmp_path)
    from app.t100_pilot_watchdog_runtime import PilotWatchdogRunner,WatchdogHalted
    scope=m.configure_v40_watch(candidate,api_key=KEY,cycles=1)
    async def placeholder():raise AssertionError()
    async def notify(_):return True
    with PilotWatchdogRunner(
        scope.journal_path,scope_sha256=scope.scope_sha256,
        observer=placeholder,notifier=notify,
    ):
        pass
    owned_candidate={**candidate,"T100_V40_WATCH_MODE":"OWNED",
        "T100_V40_WATCH_SYMBOL":"EXAMPLE_USDT",
        "T100_V40_WATCH_POSITION_ID":"900",
        "T100_V40_WATCH_ORIGIN_OID":"t100v40pilot_entry_12345",
        "T100_V40_WATCH_ENTRY_ORDER_ID":"123456",
        "T100_V40_WATCH_STOP_ID":"99",
        "T100_V40_WATCH_CONTRACTS":"250",
        "T100_V40_WATCH_MAX_STOP_PRICE":"3.5"}
    changed=m.configure_v40_watch(owned_candidate,api_key=KEY,cycles=1)
    with pytest.raises(WatchdogHalted,match="SCOPE_MISMATCH"):
        PilotWatchdogRunner(
            changed.journal_path,scope_sha256=changed.scope_sha256,
            observer=placeholder,notifier=notify,
        )
