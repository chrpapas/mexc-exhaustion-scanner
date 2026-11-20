"""Read-only current MEXC API key permissions, no trading or secrets in output.

MEXC Spot V3 GET /api/v3/apiKeyInfo reports CONTRACT_DEAL_WRITE for futures
order placement. Same physical API key/secret as the existing V40 signed GET
monitor. A successful read does NOT prove symbol allowlisting, native stop,
margin safety, live POST reachability, or confer an order permit.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass
import hashlib
import hmac
import json
import os
import time
from typing import Any, Mapping
from urllib.parse import urlencode

import httpx

_PATH="/api/v3/apiKeyInfo"
_FLAG="T100_V40_CANARY_KEY_PERMISSIONS_PREFLIGHT_ENABLED"


@dataclass(frozen=True)
class V40KeyPermissionObservation:
    status: str
    api_key_identity_verified: bool=False
    api_key_active_verified: bool=False
    futures_account_read_scope_verified: bool=False
    futures_order_read_scope_verified: bool=False
    futures_order_write_scope_verified: bool=False
    private_per_symbol_order_permission_verified: bool=False
    live_post_venue_verified: bool=False
    entry_allowed: bool=False
    live_authorization: bool=False
    mexc_posts: int=0
    database_writes: int=0


def _inspect_response(payload:Any, key:str)->V40KeyPermissionObservation:
    fail=lambda code:V40KeyPermissionObservation(code)
    if type(payload) is not dict:
        return fail("V40_KEY_INFO_RESPONSE_SHAPE_INVALID")
    body=payload.get("data",payload)
    if type(body) is not dict:
        return fail("V40_KEY_INFO_RESPONSE_SHAPE_INVALID")
    identity=body.get("accessKey",body.get("apikey"))
    if type(identity) is not str or not hmac.compare_digest(identity,key):
        return fail("V40_KEY_INFO_IDENTITY_MISMATCH")
    if body.get("status")!="VALID":
        return fail("V40_KEY_INFO_KEY_NOT_ACTIVE")
    life=body.get("remainingValidity")
    if type(life) is not int or not (life>0 or life==-999):
        return fail("V40_KEY_INFO_EXPIRED_OR_LIFETIME_UNKNOWN")
    permissions=body.get("permissions")
    if type(permissions) is not str or len(permissions)>1000:
        return fail("V40_KEY_INFO_PERMISSIONS_MISSING")
    scopes={s.strip() for s in permissions.split(",")}
    if not scopes or "" in scopes or any(
        not s.isascii() or not s.replace("_","").isupper()
        for s in scopes
    ):
        return fail("V40_KEY_INFO_PERMISSION_FORMAT_INVALID")
    account_read="CONTRACT_ACCOUNT_READ" in scopes
    order_read="CONTRACT_DEAL_READ" in scopes
    order_write="CONTRACT_DEAL_WRITE" in scopes
    return V40KeyPermissionObservation(
        status=(
            "V40_KEY_INFO_FUTURES_WRITE_SCOPE_OBSERVED_NONAUTHORIZING"
            if account_read and order_read and order_write
            else "V40_KEY_INFO_FUTURES_WRITE_SCOPE_ABSENT"
        ),
        api_key_identity_verified=True,
        api_key_active_verified=True,
        futures_account_read_scope_verified=account_read,
        futures_order_read_scope_verified=order_read,
        futures_order_write_scope_verified=order_write,
    )



async def read_v40_key_permissions_signed_get(
    *,key:str,secret:str,client:httpx.AsyncClient|None=None,
    now_ms:int|None=None,
)->V40KeyPermissionObservation:
    """READ ONLY regardless of execution mode, strictly pinned same API key.

    Called by the first-canary risk witness only after the process verifies
    its dedicated MEXC subaccount scope. Never supplies a live permit.
    """
    fail=lambda code:V40KeyPermissionObservation(code)
    if (
        type(key) is not str or len(key)<12 or key!=key.strip()
        or type(secret) is not str or len(secret)<16 or secret!=secret.strip()
    ):
        return fail("V40_KEY_INFO_CREDENTIALS_UNAVAILABLE")
    timestamp=int(time.time()*1000) if now_ms is None else now_ms
    if type(timestamp) is not int or timestamp<=0:
        return fail("V40_KEY_INFO_CLOCK_INVALID")
    # The Spot API signs the EXACT query string without the signature.
    unsigned=urlencode((("accessKey",key),("timestamp",str(timestamp))))
    signature=hmac.new(
        secret.encode("utf-8"),unsigned.encode("utf-8"),hashlib.sha256
    ).hexdigest()
    query=unsigned+"&signature="+signature
    created=client is None
    if created:
        client=httpx.AsyncClient(
            base_url="https://api.mexc.com",trust_env=False,
            follow_redirects=False,timeout=httpx.Timeout(6.0),
        )
    try:
        if str(client.base_url).rstrip("/")!="https://api.mexc.com":
            return fail("V40_KEY_INFO_HOST_UNPINNED")
        response=await client.get(
            _PATH+"?"+query,headers={"X-MEXC-APIKEY":key},
        )
        response.raise_for_status()
        return _inspect_response(response.json(),key)
    except Exception:
        # Never log signed URL, response/error body, key, or secret.
        return fail("V40_KEY_INFO_GET_UNAVAILABLE")
    finally:
        if created:
            await client.aclose()


async def observe_v40_current_key_permissions(
    env:Mapping[str,str], *, client:httpx.AsyncClient|None=None,
    now_ms:int|None=None,
)->V40KeyPermissionObservation:
    fail=lambda code:V40KeyPermissionObservation(code)
    # Existing GET-only diagnostic remains hard paper/kill isolated.
    if (
        env.get(_FLAG)!="true"
        or env.get("T100_V40_ACCOUNT_PROFILE")!="DEDICATED_WEB_PILOT"
        or env.get("T100_EXECUTION_MODE")!="paper"
        or env.get("T100_LIVE_KILL_SWITCH")!="true"
        or env.get("T100_LIVE_ORDER_API_ENABLED")!="false"
    ):
        return fail("V40_KEY_INFO_READONLY_ISOLATION_REQUIRED")
    return await read_v40_key_permissions_signed_get(
        key=env.get("MEXC_API_KEY",""),
        secret=env.get("MEXC_API_SECRET",""),
        client=client,now_ms=now_ms,
    )


async def report_from_env(env:Mapping[str,str])->V40KeyPermissionObservation:
    try:
        return await observe_v40_current_key_permissions(env)
    except Exception:
        return V40KeyPermissionObservation("V40_KEY_INFO_UNEXPECTED_HALT")


def main()->int:
    result=__import__("asyncio").run(report_from_env(dict(os.environ)))
    print(json.dumps(asdict(result),sort_keys=True),flush=True)
    return 0 if (
        result.status=="V40_KEY_INFO_FUTURES_WRITE_SCOPE_OBSERVED_NONAUTHORIZING"
    ) else 2


if __name__=="__main__":
    raise SystemExit(main())
