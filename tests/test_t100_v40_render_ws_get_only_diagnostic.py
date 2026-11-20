"""Offline V40 WS/REST monitoring tests: no exchange POST and no secrets."""
from __future__ import annotations

import asyncio
from copy import deepcopy

import pytest

from app.t100_v40_render_ws_get_only_diagnostic import observe_ws_once
from app.t100_live_guardian_readonly_once import GetOnlyMexcTransport


ENV={
    "T100_V40_WS_GET_ONLY_PROBE_ENABLED":"true",
    "T100_V40_EXECUTOR_DISABLED_WORKER":"true",
    "T100_V40_REAL_SIGNAL_MONITOR_ENABLED":"true",
    "T100_V40_MONITOR_SINGLE_POSITION_SCOPE":"true",
    "T100_EXECUTION_MODE":"paper",
    "T100_LIVE_ORDER_API_ENABLED":"false",
    "T100_LIVE_KILL_SWITCH":"true",
    "EXECUTION_ENABLED":"false",
    "TRADING_MODE":"paper",
    "MEXC_API_KEY":"x"*20,
    "MEXC_API_SECRET":"s"*32,
}

def successful_result(*,events=0,full=False):
    return {
        "read_only":True,
        "live_order_submission_possible":False,
        "entry_allowed":False,
        "live_authorization":False,
        "private_ws_pre_pilot_contract_verified":True,
        "private_ws_full_lifecycle_verified":full,
        "ws_observation":{
            "read_only":True,"entry_allowed":False,
            "live_authorization":False,
            "login_verified":True,"filter_sent":True,
            "unexpected_private_channels":[],
            "status":"COMPLETED_PRIVATE_WS_OBSERVATION",
            "private_events_seen":events,
        },
        "ws_rest_correlation":{
            "live_authorization":False,
            "private_ws_full_lifecycle_verified":full,
            "position_events_observed":1 if full else events,
            "stop_events_observed":1 if full else 0,
            "fill_events_observed":1 if full else 0,
            "position_events_correlated":1 if full else 0,
            "stop_events_correlated":1 if full else 0,
            "fill_events_correlated":1 if full else 0,
            "position_correlation_failures":0,
            "stop_correlation_failures":0,
            "fill_correlation_failures":0,
        },
    }


class NotHttp:
    def __init__(self):self.calls=[]
    async def get(self,*args,**kwargs):
        self.calls.append((args,kwargs))
        raise AssertionError("NO NETWORK EXPECTED")


def call(env=None,result=None,*,exception=False,seconds=30):
    transport=NotHttp()
    records=[]
    async def fake_lifecycle_probe(**kw):
        records.append(kw)
        assert type(kw["client"]) is GetOnlyMexcTransport
        assert not hasattr(kw["client"],"_private_post")
        assert kw["duration_seconds"]==seconds
        assert kw["connect"] is None
        if exception:
            raise RuntimeError("NEVER LOG APIKEY 0123456789")
        return deepcopy(result if result is not None else successful_result())
    observed=asyncio.run(observe_ws_once(
        dict(ENV if env is None else env),
        duration_seconds=seconds,lifecycle_probe=fake_lifecycle_probe,
        http_client=transport,
    ))
    return observed,records,transport


def test_empty_private_event_window_verifies_login_not_lifecycle():
    out,records,client=call()
    assert out.status=="V40_WS_LOGIN_FILTER_VERIFIED_LIFECYCLE_INCOMPLETE_GET_ONLY"
    assert out.login_filter_verified
    assert out.observed_private_events==0
    assert out.full_rest_correlated_lifecycle is False
    assert out.exchange_permission_certified is False
    assert out.exchange_stop_execution_certified is False
    assert out.live_authorization is False
    assert out.entry_allowed is False
    assert out.mexc_posts==out.database_writes==0
    assert len(records)==1 and client.calls==[]


def test_three_exact_correlated_private_channels_remain_non_authorizing():
    out,records,client=call(result=successful_result(events=3,full=True))
    assert out.status=="V40_WS_REST_LIFECYCLE_CORRELATED_GET_ONLY_NONAUTHORIZING"
    assert out.full_rest_correlated_lifecycle is True
    assert out.observed_private_events==3
    assert out.correlated_position_events==1
    assert out.correlated_stop_events==1
    assert out.correlated_fill_events==1
    assert out.live_authorization is False and out.entry_allowed is False
    assert out.mexc_posts==out.database_writes==0


@pytest.mark.parametrize("changed",[
    {"T100_EXECUTION_MODE":"live"},
    {"T100_LIVE_ORDER_API_ENABLED":"true"},
    {"T100_LIVE_KILL_SWITCH":"false"},
    {"EXECUTION_ENABLED":"true"},
    {"TRADING_MODE":"live"},
    {"DATABASE_URL":"postgres://somewhere"},
    {"T100_V40_TRADER_API_KEY":"not-allowed"},
    {"T100_V40_WS_GET_ONLY_PROBE_ENABLED":"false"},
    {"MEXC_API_KEY":""},
    {"MEXC_API_SECRET":""},
])
def test_non_paper_or_missing_config_halts_before_any_ws_or_get(changed):
    env=dict(ENV,**changed)
    out,records,client=call(env=env)
    assert out.status=="V40_WS_DIAGNOSTIC_PAPER_KILL_OR_SCOPE_HALT"
    assert not records and client.calls==[]
    assert not out.entry_allowed


@pytest.mark.parametrize("duration",[0,5,14,91,1000,True,30.0])
def test_invalid_duration_denied_before_connect(duration):
    out,records,client=call(seconds=duration)
    assert out.status=="V40_WS_DIAGNOSTIC_PAPER_KILL_OR_SCOPE_HALT"
    assert not records and client.calls==[]


@pytest.mark.parametrize("mutator",[
    lambda x:x.update(live_authorization=True),
    lambda x:x.update(live_order_submission_possible=True),
    lambda x:x.update(private_ws_pre_pilot_contract_verified=False),
    lambda x:x["ws_observation"].update(login_verified=False),
    lambda x:x["ws_observation"].update(unexpected_private_channels=["push.personal.unknown"]),
    lambda x:x["ws_observation"].update(private_events_seen=5),
    lambda x:x["ws_rest_correlation"].update(stop_correlation_failures=-1),
    lambda x:x.update(private_ws_full_lifecycle_verified=True),
])
def test_bad_or_contradictory_ws_rest_evidence_cannot_promote_lifecycle(mutator):
    report=successful_result()
    mutator(report)
    out,_,_=call(result=report)
    assert not out.full_rest_correlated_lifecycle
    assert not out.entry_allowed and not out.live_authorization
    assert out.mexc_posts==0


def test_failed_private_ws_never_returns_exception_message_or_creds():
    out,records,client=call(exception=True)
    assert out.status=="V40_WS_DIAGNOSTIC_NETWORK_OR_CORRELATION_HALT"
    assert "0123456789" not in str(out)
    assert not out.login_filter_verified and not out.entry_allowed
    assert out.mexc_posts==0


def test_direct_instantiation_has_no_order_submit_capability():
    from app.t100_v40_render_ws_get_only_diagnostic import V40WsGetOnlyReport
    import inspect
    import app.t100_v40_render_ws_get_only_diagnostic as m
    src=inspect.getsource(m)
    assert "._private_post(" not in src
    assert "submit_short_with_stop(" not in src
    assert "MexcTradeClient(" not in src
    assert "asyncpg." not in src
    assert V40WsGetOnlyReport("x").read_only is True
