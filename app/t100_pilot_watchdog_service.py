"""Explicit opt-in, GET-only MEXC pilot watchdog process (NOT deployed).

Runs solely for a precommitted one-contract BTC_USDT pilot origin. Does not
import the trading client, hold a POST-to-MEXC method, access Keychain, use
Render, dispatch orders or clear any safety latch. Separate read-only API key
permissions must be configured *at the exchange*; this code cannot attest
the server-side rights of the supplied key.
"""
from __future__ import annotations

import argparse
import asyncio
import hashlib
import hmac
import json
import os
import re
import sys
import time
from decimal import Decimal, InvalidOperation
from pathlib import Path
from urllib.parse import urlencode

import httpx

from app.t100_live_readonly_probe import ReadOnlyMexcProbe
from app.t100_live_ops_notifier import OpsNotice, T100LiveOpsNotifier
from app.t100_pilot_watchdog_observer import (
    PilotWatchScope, observe_pilot_watch_get,
)
from app.t100_pilot_watchdog_runtime import PilotWatchdogRunner

_MEXC_BASE = "https://api.mexc.com"
_ALLOWED_EXACT = frozenset({
    "/api/v1/private/position/open_positions",
    "/api/v1/private/stoporder/open_orders",
    "/api/v1/private/order/open_order_total_count",
})
_ALLOWED_DYNAMIC = re.compile(
    r"^/api/v1/private/order/(?:external/BTC_USDT/[A-Za-z0-9_-]{1,32}|"
    r"deal_details/[1-9][0-9]*)$"
)


class ServiceHalted(RuntimeError):
    """Redacted public-safe failure category."""


class SignedGetOnlyMexc:
    """Hard allowlist + GET-only HTTP transport (not a mutable client)."""

    def __init__(self, key: str, secret: str, *, timeout: float = 4.0):
        if not key or not secret or type(timeout) not in (int, float) or not 0 < timeout <= 10:
            raise ServiceHalted("PILOT_GET_CONFIG_INVALID")
        self._key, self._secret = key, secret
        self._http = httpx.AsyncClient(
            base_url=_MEXC_BASE, timeout=httpx.Timeout(timeout),
            follow_redirects=False, trust_env=False,
            headers={"Language": "en-US", "User-Agent": "t100-pilot-get-only/1.0"},
        )

    async def close(self):
        await self._http.aclose()

    async def _private_get(self, path: str, params=None):
        if not (path in _ALLOWED_EXACT or _ALLOWED_DYNAMIC.fullmatch(path)):
            raise ServiceHalted("PILOT_GET_ENDPOINT_NOT_ALLOWLISTED")
        if params not in (None, {}):
            # These five observer GETs require no query parameters. If
            # requests change, add reviewed signed query schema explicitly.
            raise ServiceHalted("PILOT_GET_QUERY_NOT_ALLOWLISTED")
        stamp = str(int(time.time() * 1000))
        signature = hmac.new(
            self._secret.encode(), (self._key + stamp).encode(), hashlib.sha256
        ).hexdigest()
        try:
            response = await self._http.get(
                path, headers={"ApiKey": self._key,
                               "Request-Time": stamp, "Signature": signature,
                               "Recv-Window": "10"},
            )
            response.raise_for_status()
            payload = response.json()
            if (
                type(payload) is not dict
                or payload.get("success") is not True
                or type(payload.get("code")) is not int
                or payload["code"] != 0
            ):
                raise ServiceHalted("PILOT_GET_REJECTED")
            return payload.get("data")
        except ServiceHalted:
            raise
        except Exception:
            # Never print URL, request object, account data or raw MEXC error
            raise ServiceHalted("PILOT_GET_NETWORK_OR_SHAPE_FAILED") from None


def _positive_decimal(value):
    if type(value) is not str:
        raise ServiceHalted("PILOT_SCOPE_INVALID")
    try:
        result = Decimal(value)
    except (ValueError, InvalidOperation):
        raise ServiceHalted("PILOT_SCOPE_INVALID") from None
    if not result.is_finite() or result <= 0:
        raise ServiceHalted("PILOT_SCOPE_INVALID")
    return result


def build_config(env, *, cycles):
    # Refuse to run from production/live order enabled environment.
    if (
        env.get("T100_EXECUTION_MODE") != "paper"
        or env.get("T100_LIVE_ORDER_API_ENABLED") != "false"
        or env.get("T100_LIVE_KILL_SWITCH") != "true"
    ):
        raise ServiceHalted("PILOT_PAPER_KILL_REQUIRED")
    key = env.get("T100_PILOT_READONLY_API_KEY", "")
    secret = env.get("T100_PILOT_READONLY_API_SECRET", "")
    expected_hash = env.get("T100_PILOT_EXPECTED_API_KEY_SHA256", "")
    actual_hash = hashlib.sha256(key.encode()).hexdigest()
    if (
        not key or not secret or not re.fullmatch(r"[a-f0-9]{64}", expected_hash)
        or not hmac.compare_digest(actual_hash, expected_hash)
    ):
        raise ServiceHalted("PILOT_DEDICATED_KEY_PIN_UNVERIFIED")
    oid = env.get("T100_PILOT_ORIGIN_OID", "")
    stop_id = env.get("T100_PILOT_STOP_ID", "")
    if (
        re.fullmatch(r"[A-Za-z0-9_-]{1,32}", oid) is None
        or (stop_id and re.fullmatch(r"[1-9][0-9]*", stop_id) is None)
    ):
        raise ServiceHalted("PILOT_SCOPE_INVALID")
    price = _positive_decimal(env.get("T100_PILOT_MAX_STOP_PRICE"))
    webhook = env.get("T100_PILOT_DISCORD_WEBHOOK_URL", "")
    # Require a separately configured webhook; do not reuse production alerts
    if not webhook.startswith("https://discord.com/api/webhooks/"):
        raise ServiceHalted("PILOT_DISCORD_WEBHOOK_REQUIRED")
    journal = env.get("T100_PILOT_WATCHDOG_JOURNAL", "")
    if not journal or not Path(journal).is_absolute():
        raise ServiceHalted("PILOT_ABSOLUTE_JOURNAL_REQUIRED")
    if cycles is not None and (type(cycles) is not int or cycles < 1):
        raise ServiceHalted("PILOT_INVALID_CYCLES")
    scope = PilotWatchScope(
        account=actual_hash, origin_oid=oid, stop_id=stop_id or None,
        maximum_stop_price=price, alert_after_ms=3000,
    )
    # Bind journal to key fingerprint AND immutable order/stop/price scope.
    scope_hash = hashlib.sha256(
        json.dumps([actual_hash, oid, stop_id, str(price), "BTC_USDT", 1],
                   separators=(",", ":")).encode()
    ).hexdigest()
    return key, secret, webhook, journal, scope, scope_hash


async def run_service(*, environ=None, cycles: int | None = 1):
    env = os.environ if environ is None else environ
    key, secret, webhook, journal, scope, scope_hash = build_config(env, cycles=cycles)
    client = SignedGetOnlyMexc(key, secret)
    notifier = T100LiveOpsNotifier(webhook, cooldown_seconds=0)
    # Use an explicit async-only notification callback; Discord POST is not
    # a MEXC mutation and never contains account/order/price identifiers.
    async def send_alert(code):
        if not re.fullmatch(r"WATCH_[A-Z0-9_]{1,90}", code):
            return False
        return await notifier.send(
            OpsNotice(key="pilot-watchdog-" + code, title="T100 pilot watchdog alarm",
                      description=code, severity="critical"),
            force=True,
        )
    try:
        probe = ReadOnlyMexcProbe(client)
        async def observe():
            return await observe_pilot_watch_get(probe, scope)
        with PilotWatchdogRunner(
            journal, scope_sha256=scope_hash, observer=observe,
            notifier=send_alert, poll_seconds=1.0,
            observation_timeout_seconds=8.0,
            notification_timeout_seconds=8.0,
        ) as runner:
            return await runner.run(max_cycles=cycles)
    finally:
        await notifier.close()
        await client.close()


def main():
    parser = argparse.ArgumentParser(
        description="Explicit GET-only BTC pilot monitor; NOT an order executor"
    )
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--cycles", type=int, default=1)
    mode.add_argument("--continuous", action="store_true")
    mode.add_argument("--print-sentinel-scope", action="store_true")
    args = parser.parse_args()
    if args.print_sentinel_scope:
        try:
            config = build_config(os.environ, cycles=1)
        except ServiceHalted as exc:
            print("PILOT WATCHDOG HALTED:", str(exc))
            sys.exit(2)
        # A hash of the precommitted scope, never a key or account ID.
        # This is local setup metadata; do not publish it in repository logs.
        print("PILOT_SENTINEL_SCOPE_SHA256=" + config[5])
        print("NO NETWORK OR JOURNAL OPERATIONS. NO MEXC ORDERS.")
        return
    requested_cycles = None if args.continuous else args.cycles
    try:
        outcome = asyncio.run(run_service(cycles=requested_cycles))
    except Exception as exc:
        print("PILOT WATCHDOG HALTED:",
              str(exc) if isinstance(exc, ServiceHalted) else "PILOT_RUNTIME_UNAVAILABLE")
        sys.exit(2)
    print("PILOT WATCHDOG RESULT:", outcome.status)
    print("NO MEXC ORDERS. PRODUCTION REMAINS DISABLED.")
    if outcome.alarm_latched or outcome.notifications_pending:
        print("PILOT WATCHDOG ALARM LATCHED OR DELIVERY PENDING")
        sys.exit(2)


if __name__ == "__main__":
    main()
