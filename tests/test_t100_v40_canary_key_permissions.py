"""MEXC V40 trade-permission diagnostic never submits an order (offline)."""
import asyncio
from dataclasses import asdict
import hashlib
import hmac
from urllib.parse import parse_qs, urlencode

import httpx
import pytest

from app.t100_v40_canary_key_permissions import (
    V40KeyPermissionObservation, observe_v40_current_key_permissions,
)

KEY="EXAMPLE_MEXC_API_KEY_NO_REAL_FUNDS"
SECRET="EXAMPLE_MEXC_SECRET_NO_REAL_FUNDS"
SCOPES="SPOT_ACCOUNT_READ,CONTRACT_ACCOUNT_READ,CONTRACT_DEAL_READ,CONTRACT_DEAL_WRITE"


def env(**overrides):
    values={
        "T100_V40_CANARY_KEY_PERMISSIONS_PREFLIGHT_ENABLED":"true",
        "T100_V40_ACCOUNT_PROFILE":"DEDICATED_WEB_PILOT",
        "T100_EXECUTION_MODE":"paper",
        "T100_LIVE_KILL_SWITCH":"true",
        "T100_LIVE_ORDER_API_ENABLED":"false",
        "MEXC_API_KEY":KEY,
        "MEXC_API_SECRET":SECRET,
    }
    values.update(overrides)
    return values


def response(**overrides):
    payload={
        "accessKey":KEY,"status":"VALID",
        "remainingValidity":22,"permissions":SCOPES,
    }
    payload.update(overrides)
    return payload


def call(payload=None,*,settings=None,status=200):
    requests=[]
    def mock(request):
        requests.append(request)
        return httpx.Response(status,json=response() if payload is None else payload)
    async def run():
        async with httpx.AsyncClient(
            base_url="https://api.mexc.com",
            transport=httpx.MockTransport(mock),
        ) as client:
            result=await observe_v40_current_key_permissions(
                env() if settings is None else settings,
                client=client,now_ms=1770000000000,
            )
            return result
    return asyncio.run(run()),requests


def test_signed_readonly_permission_query_checks_actual_key_and_write_scope():
    result,requests=call()
    assert type(result) is V40KeyPermissionObservation
    assert result.status=="V40_KEY_INFO_FUTURES_WRITE_SCOPE_OBSERVED_NONAUTHORIZING"
    assert result.api_key_identity_verified and result.api_key_active_verified
    assert result.futures_order_write_scope_verified
    assert result.futures_order_read_scope_verified
    assert result.futures_account_read_scope_verified
    assert result.private_per_symbol_order_permission_verified is False
    assert result.live_post_venue_verified is False
    assert result.live_authorization is False and result.entry_allowed is False
    assert result.mexc_posts==0 and result.database_writes==0
    assert len(requests)==1
    request=requests[0]
    assert request.method=="GET"
    assert request.url.host=="api.mexc.com"
    assert request.url.path=="/api/v3/apiKeyInfo"
    assert request.headers["X-MEXC-APIKEY"]==KEY
    query=parse_qs(request.url.query.decode())
    assert query["accessKey"]==[KEY]
    assert query["timestamp"]==["1770000000000"]
    plain=urlencode((("accessKey",KEY),("timestamp","1770000000000")))
    want=hmac.new(SECRET.encode(),plain.encode(),hashlib.sha256).hexdigest()
    assert query["signature"]==[want]
    assert "SECRET" not in repr(result)


@pytest.mark.parametrize("payload,expected",[
    (response(permissions="CONTRACT_ACCOUNT_READ,CONTRACT_DEAL_READ"),
     "V40_KEY_INFO_FUTURES_WRITE_SCOPE_ABSENT"),
    (response(accessKey="OTHER_ACCOUNT"),
     "V40_KEY_INFO_IDENTITY_MISMATCH"),
    (response(status="FROZEN"),"V40_KEY_INFO_KEY_NOT_ACTIVE"),
    (response(remainingValidity=0),"V40_KEY_INFO_EXPIRED_OR_LIFETIME_UNKNOWN"),
    (response(remainingValidity="12"),"V40_KEY_INFO_EXPIRED_OR_LIFETIME_UNKNOWN"),
    (response(permissions=[]),"V40_KEY_INFO_PERMISSIONS_MISSING"),
    ({"data":None},"V40_KEY_INFO_RESPONSE_SHAPE_INVALID"),
])
def test_missing_scopes_or_wrong_key_never_claim_permission(payload,expected):
    r,requests=call(payload)
    assert len(requests)==1 and requests[0].method=="GET"
    assert r.status==expected
    assert not r.live_authorization and not r.entry_allowed
    assert r.mexc_posts==0


@pytest.mark.parametrize("override",[
    {"T100_EXECUTION_MODE":"live"},
    {"T100_LIVE_KILL_SWITCH":"false"},
    {"T100_LIVE_ORDER_API_ENABLED":"true"},
    {"T100_V40_ACCOUNT_PROFILE":"BOXWAVE"},
    {"T100_V40_CANARY_KEY_PERMISSIONS_PREFLIGHT_ENABLED":"false"},
    {"MEXC_API_SECRET":""},
])
def test_unsafe_or_missing_profile_fails_before_network(override):
    result,requests=call(settings=env(**override))
    assert result.status in (
        "V40_KEY_INFO_READONLY_ISOLATION_REQUIRED",
        "V40_KEY_INFO_CREDENTIALS_UNAVAILABLE",
    )
    assert not requests


def test_http_failure_never_exposes_body_or_secrets():
    result,requests=call(
        {"error":SECRET,"private":KEY},status=403,
    )
    assert result.status=="V40_KEY_INFO_GET_UNAVAILABLE"
    assert SECRET not in repr(result)
    assert KEY not in repr(result)
    assert len(requests)==1


def test_official_nested_data_is_accepted_but_still_nonauthorizing():
    result,_=call({"success":True,"code":0,"data":response()})
    assert result.futures_order_write_scope_verified
    assert not result.live_authorization


def test_same_account_key_info_get_can_be_used_by_live_risk_witness_without_post():
    from app.t100_v40_canary_key_permissions import (
        read_v40_key_permissions_signed_get,
    )
    requests=[]
    def mock(request):
        requests.append(request)
        return httpx.Response(200,json=response())
    async def run():
        async with httpx.AsyncClient(
            base_url="https://api.mexc.com",
            transport=httpx.MockTransport(mock),
        ) as http:
            return await read_v40_key_permissions_signed_get(
                key=KEY,secret=SECRET,client=http,now_ms=1770000000000,
            )
    result=asyncio.run(run())
    assert result.futures_order_write_scope_verified is True
    assert result.live_authorization is False
    assert result.entry_allowed is False
    assert result.mexc_posts==0
    assert len(requests)==1 and requests[0].method=="GET"
