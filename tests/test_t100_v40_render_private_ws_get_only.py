"""No-order V40 private WS/REST preflight unit tests. No external network."""
import asyncio
from dataclasses import replace

import pytest

import app.t100_v40_render_private_ws_get_only as m
from tests.test_t100_v40_render_supervised_pilot_worker import BASE


def response(**change):
    ws={
        "status":"COMPLETED_PRIVATE_WS_OBSERVATION",
        "login_verified":True,"filter_sent":True,"read_only":True,
        "live_authorization":False,"entry_allowed":False,
        "unexpected_private_channels":[],
        "private_events_seen":0,
        "payload_semantic_events_verified":0,
        "payload_semantic_failures":0,
    }
    corr={
        "position_events_correlated":0,"stop_events_correlated":0,
        "fill_events_correlated":0,"position_correlation_failures":0,
        "stop_correlation_failures":0,"fill_correlation_failures":0,
        "private_ws_full_lifecycle_verified":False,
        "live_authorization":False,
    }
    report={
        "read_only":True,"live_order_submission_possible":False,
        "entry_allowed":False,"live_authorization":False,
        "ws_observation":ws,"ws_rest_correlation":corr,
    }
    report.update(change)
    return report


def test_readonly_ws_login_does_not_certify_stop_guardian_or_trade():
    r=m.assess_ws_rest_preflight(response())
    assert r.status=="V40_WS_LOGIN_FILTER_OBSERVED_LIFECYCLE_NOT_CERTIFIED"
    assert r.ws_login_observed and r.ws_filter_observed
    assert r.ws_semantic_events==0
    assert not r.full_lifecycle_certified
    assert not r.independent_guardian_connected
    assert not r.native_stop_execution_certified
    assert not r.live_adapter_connected
    assert not r.account_wide_completeness_certified
    assert r.exchange_posts==0 and r.database_writes==0
    assert not r.entry_allowed and not r.live_authorization


def test_all_three_correlated_channels_are_still_non_authorizing():
    r0=response()
    r0["ws_rest_correlation"].update(
        private_ws_full_lifecycle_verified=True,
        position_events_correlated=1,stop_events_correlated=1,
        fill_events_correlated=1,
    )
    r0["ws_observation"].update(
        private_events_seen=3,payload_semantic_events_verified=3,
    )
    out=m.assess_ws_rest_preflight(r0)
    assert out.status=="V40_WS_REST_LIFECYCLE_CORRELATED_OBSERVED_NONAUTHORIZING"
    assert not out.full_lifecycle_certified
    assert not out.entry_allowed and not out.live_authorization
    assert not out.native_stop_execution_certified


@pytest.mark.parametrize("kind",[
    "wrong_route","post_enabled","auth_true","missing_ws",
    "missing_corr","bad_login","wrong_filter","unexpected_channel",
    "semantics_failure","negative_correlation","boolean_event_count",
])
def test_synthetic_corruption_never_certifies_private_ws(kind):
    row=response()
    if kind=="wrong_route":
        row["read_only"]=False
    elif kind=="post_enabled":
        row["live_order_submission_possible"]=True
    elif kind=="auth_true":
        row["live_authorization"]=True
    elif kind=="missing_ws":
        row.pop("ws_observation")
    elif kind=="missing_corr":
        row.pop("ws_rest_correlation")
    elif kind=="bad_login":
        row["ws_observation"]["login_verified"]=False
    elif kind=="wrong_filter":
        row["ws_observation"]["filter_sent"]=False
    elif kind=="unexpected_channel":
        row["ws_observation"]["unexpected_private_channels"]=["push.personal.account"]
    elif kind=="semantics_failure":
        row["ws_observation"]["payload_semantic_failures"]=1
    elif kind=="negative_correlation":
        row["ws_rest_correlation"]["fill_events_correlated"]=-1
    else:
        row["ws_observation"]["private_events_seen"]=True
    x=m.assess_ws_rest_preflight(row)
    assert x.status.endswith("_HALT")
    assert not x.ws_login_observed
    assert not x.full_lifecycle_certified
    assert x.exchange_posts==0 and x.database_writes==0
    assert not x.entry_allowed and not x.live_authorization


@pytest.mark.parametrize("flags",[
    {"T100_V40_PRIVATE_WS_GET_ONLY_PREFLIGHT_ENABLED":"false"},
    {"T100_EXECUTION_MODE":"live"},
    {"T100_LIVE_ORDER_API_ENABLED":"true"},
    {"T100_LIVE_KILL_SWITCH":"false"},
    {"T100_V40_EXECUTOR_DISABLED_WORKER":"false"},
    {"T100_V40_MONITOR_SINGLE_POSITION_SCOPE":"false"},
])
def test_misconfigured_scope_blocks_before_callback_or_mexc_connection(flags):
    env={
        **BASE,"T100_V40_PRIVATE_WS_GET_ONLY_PREFLIGHT_ENABLED":"true",
        **flags,
    }
    calls=[]
    async def fake(**kwargs):
        calls.append(kwargs)
        return response()
    out=asyncio.run(m.observe_v40_private_ws_get_only(env,observe=fake))
    assert out.status=="V40_WS_PAPER_KILL_OR_READONLY_SCOPE_REQUIRED_HALT"
    assert calls==[]
    assert out.exchange_posts==0 and out.database_writes==0


def test_readonly_http_client_has_no_exchange_post_and_allows_only_numeric_deals():
    from app.t100_live_guardian_readonly_once import GetOnlyMexcTransport
    assert GetOnlyMexcTransport._allowed("/api/v1/private/order/deal_details/123")
    assert not GetOnlyMexcTransport._allowed("/api/v1/private/order/deal_details/0")
    assert not GetOnlyMexcTransport._allowed("/api/v1/private/order/deal_details/-1")
    assert not GetOnlyMexcTransport._allowed("/api/v1/private/stoporder/change_plan_price")
    assert not hasattr(GetOnlyMexcTransport,"_private_post")


def test_opt_in_ws_get_only_invokes_once_and_redacts_network_exceptions():
    env={**BASE,"T100_V40_PRIVATE_WS_GET_ONLY_PREFLIGHT_ENABLED":"true"}
    calls=[]
    async def fake(*,client,api_key,api_secret,duration_seconds):
        calls.append(1)
        assert not hasattr(client,"_private_post")
        assert duration_seconds==20
        assert api_key==BASE["MEXC_API_KEY"]
        assert api_secret==BASE["MEXC_API_SECRET"]
        return response()
    result=asyncio.run(m.observe_v40_private_ws_get_only(env,observe=fake))
    assert len(calls)==1
    assert result.ws_login_observed
    assert result.exchange_posts==0
    secret="SUPER_SECRET_NETWORK_MESSAGE"
    async def fails(**kwargs):
        raise RuntimeError(secret)
    failure=asyncio.run(m.observe_v40_private_ws_get_only(env,observe=fails))
    assert failure.status=="V40_WS_SIGNED_GET_OR_PRIVATE_WS_OBSERVATION_FAILED_HALT"
    assert secret not in str(failure)
    assert failure.exchange_posts==0 and failure.database_writes==0
