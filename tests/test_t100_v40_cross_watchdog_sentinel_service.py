"""No-network local V40 sentinel configuration and secrets isolation."""
from __future__ import annotations

import asyncio
from pathlib import Path
from unittest.mock import AsyncMock

import pytest

import app.t100_v40_cross_watchdog_sentinel_service as m


def env(tmp_path):
    return {
        "T100_EXECUTION_MODE":"paper",
        "T100_LIVE_ORDER_API_ENABLED":"false",
        "T100_LIVE_KILL_SWITCH":"true",
        "T100_V40_SENTINEL_ENABLED":"true",
        "T100_V40_SENTINEL_DISCORD_KEYCHAIN_SERVICE":m.SENTINEL_KEYCHAIN,
        "T100_V40_SENTINEL_SCOPE_SHA256":"a"*64,
        "T100_V40_SENTINEL_WATCHDOG_JOURNAL":str(tmp_path/"watch.sqlite"),
        "T100_V40_SENTINEL_OUTBOX_JOURNAL":str(tmp_path/"sentinel.sqlite"),
    }


def test_config_requires_separate_abs_journals_and_scope(tmp_path):
    watched,outbox,scope=m.config(env(tmp_path),cycles=1)
    assert watched!=outbox and watched.is_absolute() and outbox.is_absolute()
    assert scope=="a"*64
    bad={**env(tmp_path),
         "T100_V40_SENTINEL_OUTBOX_JOURNAL":str(watched)}
    with pytest.raises(m.V40SentinelHalted,match="JOURNALS"):
        m.config(bad,cycles=1)
    bad={**env(tmp_path),"T100_V40_SENTINEL_SCOPE_SHA256":"INVALID"}
    with pytest.raises(m.V40SentinelHalted,match="SCOPE"):
        m.config(bad,cycles=1)


@pytest.mark.parametrize("field,value",[
    ("T100_EXECUTION_MODE","live"),
    ("T100_LIVE_ORDER_API_ENABLED","true"),
    ("T100_LIVE_KILL_SWITCH","false"),
    ("T100_V40_SENTINEL_ENABLED","false"),
    ("T100_V40_SENTINEL_DISCORD_KEYCHAIN_SERVICE","wrong"),
])
def test_rejects_mutation_mode_before_keychain(tmp_path,field,value,monkeypatch):
    calls=[]
    monkeypatch.setattr(m,"_from_keychain",lambda *a:(
        calls.append(a) or "SECRET",
    ))
    with pytest.raises(m.V40SentinelHalted):
        asyncio.run(m.run_sentinel(
            environ={**env(tmp_path),field:value},cycles=1,
        ))
    assert calls==[]


def test_distinct_webhooks_are_required(tmp_path,monkeypatch):
    webhook="https://discord.com/api/webhooks/123456/abcDEF0123"
    monkeypatch.setattr(m,"_from_keychain",lambda *a:webhook)
    with pytest.raises(m.V40SentinelHalted,match="INDEPENDENT_DISCORD"):
        asyncio.run(m.run_sentinel(environ=env(tmp_path),cycles=1))


def test_primary_and_secondary_keys_are_not_mexc_credentials(tmp_path,monkeypatch):
    services=[]
    def local_secret(service,account):
        services.append((service,account))
        return (
            "https://discord.com/api/webhooks/123456/abcDEF0123"
            if service==m.KEYCHAIN_DISCORD
            else "https://discord.com/api/webhooks/654321/xyzABC9876"
        )
    monkeypatch.setattr(m,"_from_keychain",local_secret)
    class FakeNotifier:
        def __init__(self,*a,**kw):pass
        async def send(self,*a,**kw):return True
        async def close(self):pass
    events=[]
    class FakeSentinel:
        def __init__(self,**kw):
            events.append(kw)
        def __enter__(self):return self
        def __exit__(self,*_):pass
        async def run(self,*,max_cycles):
            events.append(max_cycles)
            return type("Result",(),{"status":"WATCH_SENTINEL_TEST_OK",
               "alarm_latched":False,"notifications_pending":0})()
    monkeypatch.setattr(m,"T100LiveOpsNotifier",FakeNotifier)
    monkeypatch.setattr(m,"PilotLivenessSentinel",FakeSentinel)
    outcome=asyncio.run(m.run_sentinel(environ=env(tmp_path),cycles=1))
    assert outcome.status=="WATCH_SENTINEL_TEST_OK"
    assert services==[
        (m.KEYCHAIN_DISCORD,m.DISCORD_ACCOUNT),
        (m.SENTINEL_KEYCHAIN,m.DISCORD_ACCOUNT),
    ]
    assert events[0]["stale_after_ms"]==30000
    assert events[0]["poll_seconds"]==5.0
    assert events[1]==1
