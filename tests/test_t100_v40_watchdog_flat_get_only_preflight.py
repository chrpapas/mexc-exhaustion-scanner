"""Offline regression: Mac V40 flat probe uses GETs only, no journal/Discord."""
from __future__ import annotations

import asyncio
from copy import deepcopy

import pytest

import scripts.t100_v40_watchdog_flat_get_only_preflight as m
from app.t100_v40_cross_watchdog_service import KEYCHAIN_READONLY


PAPER={
    "T100_EXECUTION_MODE":"paper",
    "T100_LIVE_ORDER_API_ENABLED":"false",
    "T100_LIVE_KILL_SWITCH":"true",
}


def test_real_get_path_is_read_only_and_does_not_open_journal_or_discord(monkeypatch):
    paths=[]
    key_reads=[]
    class FakeClient:
        def __init__(self,key,secret):
            assert key=="testing-readonly-key-1234"
            assert secret=="testing-private-secret-5678"
        async def close(self):
            paths.append("CLOSE")
        async def _private_get(self,path,params=None):
            assert params is None
            paths.append(path)
            payload={
                "/api/v1/private/position/open_positions":[],
                "/api/v1/private/stoporder/open_orders":[],
                "/api/v1/private/order/open_order_total_count":{
                    "sumCount":0,"limitOrderCount":0,"stopOrderCount":0,
                    "planOrderCount":0,"trackOrderCount":0,
                },
                "/api/v1/private/position/position_mode":{"positionMode":1},
            }
            return deepcopy(payload[path])

    def keychain(service,account):
        key_reads.append((service,account))
        assert service==KEYCHAIN_READONLY
        return ("testing-readonly-key-1234" if account==m.KEY_ACCOUNT
                else "testing-private-secret-5678")
    monkeypatch.setattr(m,"_from_keychain",keychain)
    monkeypatch.setattr(m,"V40WatchSignedGetOnly",FakeClient)
    result=asyncio.run(m.probe_flat(
        keychain_service=KEYCHAIN_READONLY,env=PAPER,
    ))
    assert result.status=="WATCH_FLAT_INVENTORY_OBSERVED_NONAUTHORIZING"
    assert result.flat_inventory_observed is True
    assert result.exchange_side_readonly_permission_verified is False
    assert result.account_scope_independently_verified is False
    assert result.continuous_monitor_running is False
    assert result.entry_allowed is False and result.live_authorization is False
    assert result.database_writes==result.mexc_posts==0
    assert result.discord_requests==result.journal_writes==0
    assert len([x for x in paths if x!="CLOSE"])==8
    assert paths[-1]=="CLOSE"
    assert len(key_reads)==2


@pytest.mark.parametrize("change",[
    {"T100_EXECUTION_MODE":"live"},
    {"T100_LIVE_ORDER_API_ENABLED":"true"},
    {"T100_LIVE_KILL_SWITCH":"false"},
])
def test_paper_kill_required_before_any_keychain_reads(change,monkeypatch):
    calls=[]
    monkeypatch.setattr(m,"_from_keychain",lambda *a:(
        calls.append(a) or "SENSITIVE_TEST_SECRET"
    ))
    result=asyncio.run(m.probe_flat(
        keychain_service=KEYCHAIN_READONLY,env={**PAPER,**change},
    ))
    assert result.status=="WATCH_V40_FLAT_GET_SCOPE_OR_KILL_INVALID_HALT"
    assert result.read_only and not result.flat_inventory_observed
    assert calls==[]


def test_dedicated_keychain_scope_only_not_trader_or_boxwave(monkeypatch):
    calls=[]
    monkeypatch.setattr(m,"_from_keychain",lambda *a:(
        calls.append(a) or "SENSITIVE_TEST_SECRET"
    ))
    result=asyncio.run(m.probe_flat(
        keychain_service="t100225-mexc-web-pilot-subaccount-v1",env=PAPER,
    ))
    assert result.status=="WATCH_V40_FLAT_GET_SCOPE_OR_KILL_INVALID_HALT"
    assert calls==[]


def test_keychain_missing_fails_closed_with_no_network(monkeypatch):
    def error(*a):
        raise RuntimeError("DO_NOT_PRINT_THIS_SECRET")
    monkeypatch.setattr(m,"_from_keychain",error)
    result=asyncio.run(m.probe_flat(
        keychain_service=KEYCHAIN_READONLY,env=PAPER,
    ))
    assert result.status=="WATCH_V40_READONLY_KEYCHAIN_UNVERIFIED_HALT"
    assert "DO_NOT_PRINT_THIS_SECRET" not in str(result)
    assert result.mexc_posts==result.database_writes==0
