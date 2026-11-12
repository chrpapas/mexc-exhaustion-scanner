"""Opt-in one-cycle T100-owned guardian, with SELECT-only DB and signed GET-only MEXC.

Not deployed. Has NO exchange POST method, order gateway, DB write command,
Discord outbound channel, or automatic adoption of manual positions.
Default CLI invocation is an offline wiring self-check (no network).

To run against a real dedicated account, a separately provisioned restricted
DB role and a pinned account/position are REQUIRED. Missing configuration
halts before any connection. No auto-migration or service mutation.
"""
from __future__ import annotations

import argparse
import asyncio
from dataclasses import dataclass
import hashlib
import hmac
import json
import os
import re
import ssl
import time
from typing import Any, Mapping
from urllib.parse import urlencode

from app.t100_live_guardian_checkpoint_reader import (
    load_pinned_owned_checkpoint, GuardianCheckpointHalted,
)
from app.t100_live_guardian_observer import observe_owned_read_only
from app.t100_live_readonly_probe import ReadOnlyMexcProbe


class GuardianOnceHalted(RuntimeError):
    pass


@dataclass(frozen=True)
class ReadOnlyCycleSummary:
    status: str
    managed_position_observed: bool = False
    native_market_stop_verified: bool = False
    foreign_position_count: int = 0
    alert: bool = True
    entry_allowed: bool = False
    live_authorization: bool = False
    emergency_close_allowed: bool = False
    mexc_orders_sent: int = 0


class GetOnlyMexcTransport:
    """Restricted GET protocol; deliberately does NOT implement _private_post."""

    _FIXED = frozenset({
        "/api/v1/private/order/open_order_total_count",
        "/api/v1/private/position/open_positions",
        "/api/v1/private/position/position_mode",
        "/api/v1/private/stoporder/open_orders",
        "/api/v1/private/stoporder/list/orders",
        "/api/v1/private/position/list/history_positions",
    })
    _EXTERNAL = re.compile(
        r"/api/v1/private/order/external/[A-Z0-9]+_USDT/[A-Za-z0-9_-]{1,128}"
    )
    _ORDER = re.compile(r"/api/v1/private/order/get/[1-9][0-9]*")
    _ORDER_DEALS = re.compile(
        r"/api/v1/private/order/deal_details/[1-9][0-9]*"
    )
    # Official MEXC Futures API domain since January 2026. Do not silently
    # fall back to retired contract.mexc.com or follow credentialed redirects.
    BASE_URL = "https://api.mexc.com"

    def __init__(self, client: Any, *, api_key: str, api_secret: str):
        if (type(api_key) is not str or len(api_key) < 12 or
                type(api_secret) is not str or len(api_secret) < 16):
            raise GuardianOnceHalted("SIGNED_GET_CREDENTIALS_NOT_CONFIGURED")
        self._client = client
        self._key = api_key
        self._secret = api_secret

    @classmethod
    def _allowed(cls, path: str) -> bool:
        return (type(path) is str and
                (path in cls._FIXED or
                 cls._EXTERNAL.fullmatch(path) is not None or
                 cls._ORDER.fullmatch(path) is not None or
                 cls._ORDER_DEALS.fullmatch(path) is not None))

    async def _private_get(
        self, path: str, params: dict[str, Any] | None = None,
    ) -> Any:
        if not self._allowed(path):
            raise GuardianOnceHalted("MEXC_GET_ENDPOINT_NOT_ALLOWLISTED")
        if params is not None and (
                type(params) is not dict or
                any(type(k) is not str or type(v) not in (str, int)
                    for k, v in params.items())):
            raise GuardianOnceHalted("MEXC_GET_QUERY_NOT_CANONICAL")
        pairs = sorted((params or {}).items())
        query_string = urlencode(pairs)
        stamp = str(int(time.time() * 1000))
        signature = hmac.new(
            self._secret.encode(),
            f"{self._key}{stamp}{query_string}".encode(),
            hashlib.sha256,
        ).hexdigest()
        try:
            response = await self._client.get(
                path, params=pairs,
                headers={
                    "ApiKey": self._key,
                    "Request-Time": stamp,
                    "Signature": signature,
                    "Recv-Window": "10",
                    "Language": "en-US",
                },
            )
            response.raise_for_status()
            payload = response.json()
        except Exception:
            raise GuardianOnceHalted("MEXC_SIGNED_GET_TRANSPORT_FAILED") from None
        if (type(payload) is not dict or
                payload.get("success") is not True or
                type(payload.get("code")) is not int or
                payload["code"] != 0):
            raise GuardianOnceHalted("MEXC_SIGNED_GET_EXPLICITLY_REJECTED")
        return payload.get("data")


def validate_once_config(env: Mapping[str, str]) -> dict[str, str]:
    """Require an isolated observer, not a live execution environment."""
    if (env.get("T100_GUARDIAN_OBSERVER_ENABLED") != "true" or
            env.get("T100_EXECUTION_MODE", "paper") != "paper" or
            env.get("T100_LIVE_ORDER_API_ENABLED", "false") != "false" or
            env.get("T100_LIVE_KILL_SWITCH", "true") != "true"):
        raise GuardianOnceHalted("READ_ONLY_OBSERVER_MODE_NOT_GATED")
    key = env.get("T100_GUARDIAN_MEXC_API_KEY", "")
    secret = env.get("T100_GUARDIAN_MEXC_API_SECRET", "")
    scope = env.get("T100_GUARDIAN_ACCOUNT_KEY_SHA256", "")
    position = env.get("T100_GUARDIAN_POSITION_ID", "")
    dsn = env.get("T100_GUARDIAN_READONLY_DB_DSN", "")
    ca_file = env.get("T100_GUARDIAN_DB_CA_FILE", "")
    if (not key or not secret or
            type(scope) is not str or not re.fullmatch("[a-f0-9]{64}", scope) or
            type(position) is not str or
            not re.fullmatch("[1-9][0-9]*", position) or
            not dsn.startswith(("postgresql://","postgres://")) or
            not ca_file):
        raise GuardianOnceHalted("READ_ONLY_OBSERVER_CONFIG_INCOMPLETE")
    from app.t100_live_guardian_checkpoint_reader import check_api_scope
    check_api_scope(api_key=key, pinned_scope_sha256=scope)
    return {
        "api_key": key,
        "api_secret": secret,
        "scope": scope,
        "position_id": position,
        "dsn": dsn,
        "ca_file": ca_file,
    }


async def inspect_once(
    *, conn: Any, api_key: str, pinned_scope_sha256: str,
    pinned_position_id: str, mexc_probe: ReadOnlyMexcProbe,
) -> ReadOnlyCycleSummary:
    ownership = await load_pinned_owned_checkpoint(
        conn, api_key=api_key,
        pinned_scope_sha256=pinned_scope_sha256,
        pinned_position_id=pinned_position_id,
    )
    if ownership.owned is None:
        return ReadOnlyCycleSummary(
            status="NO_MANAGED_CHECKPOINT_NO_EXCHANGE_ACTION",
            alert=False,
        )
    status = await observe_owned_read_only(
        owned=ownership.owned,
        actual_scope=pinned_scope_sha256,
        probe=mexc_probe,
    )
    return ReadOnlyCycleSummary(
        status=status.status,
        managed_position_observed=status.owned_position_seen,
        native_market_stop_verified=status.native_market_stop_verified,
        foreign_position_count=status.foreign_position_count,
        alert=status.alert,
    )


async def run_with_network(env: Mapping[str, str]) -> ReadOnlyCycleSummary:
    config = validate_once_config(env)
    # Imported only AFTER the read-only configuration is checked.
    import asyncpg
    import httpx

    # TLS must verify hostname and certificate against an explicitly pinned
    # CA file; never allow sslmode=disable or insecure HTTP.
    context = ssl.create_default_context(cafile=config["ca_file"])
    context.check_hostname = True
    context.verify_mode = ssl.CERT_REQUIRED

    conn = None
    try:
        conn = await asyncpg.connect(
            config["dsn"], timeout=8, command_timeout=15, ssl=context,
        )
        async with conn.transaction(readonly=True):
            async with httpx.AsyncClient(
                base_url=GetOnlyMexcTransport.BASE_URL,
                timeout=httpx.Timeout(20.0),
                follow_redirects=False,
            ) as http:
                get_only = GetOnlyMexcTransport(
                    http, api_key=config["api_key"],
                    api_secret=config["api_secret"],
                )
                return await inspect_once(
                    conn=conn, api_key=config["api_key"],
                    pinned_scope_sha256=config["scope"],
                    pinned_position_id=config["position_id"],
                    mexc_probe=ReadOnlyMexcProbe(get_only),
                )
    finally:
        if conn is not None:
            await conn.close()


def main() -> int:
    parser = argparse.ArgumentParser(description="T100 read-only guardian once")
    parser.add_argument("--once", action="store_true",
                        help="Opt-in real signed GET + SELECT-only observation")
    args = parser.parse_args()
    if not args.once:
        print(json.dumps({
            "status": "OBSERVER_MODULE_INSTALLED_NOT_CONNECTED",
            "exchange_mutations": 0,
            "db_mutations": 0,
            "live_authorization": False,
        },sort_keys=True))
        return 0
    try:
        report = asyncio.run(run_with_network(os.environ))
    except Exception as exc:
        # Never display exception/URL/HTTP headers: environment may contain
        # credentials, DB DSN and account or position identifiers.
        print(json.dumps({
            "status": "OBSERVER_HALTED_FAIL_CLOSED",
            "error_class": type(exc).__name__,
            "exchange_mutations": 0,
            "db_mutations": 0,
            "live_authorization": False,
        },sort_keys=True))
        return 2
    from dataclasses import asdict
    print(json.dumps(asdict(report),sort_keys=True))
    return 0 if not report.alert else 1


if __name__ == "__main__":
    raise SystemExit(main())
