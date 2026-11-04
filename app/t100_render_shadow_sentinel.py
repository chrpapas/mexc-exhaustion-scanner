"""Independent Render worker: read-only PostgreSQL shadow-witness sentinel.

No MEXC imports, API keys, trade-client code, table creation, account writes
or automatic emergency closes. Requires a separate SELECT-only DB role and
dedicated Discord webhook; a process crash loses in-memory notification
deduplication. A second host and durable alert outbox are still release gates.
"""
from __future__ import annotations

import argparse
import asyncio
import os
import re
import sys
from dataclasses import dataclass

from app.t100_live_ops_notifier import OpsNotice, T100LiveOpsNotifier
from app.t100_render_shadow_witness import (
    RenderShadowWitness, ShadowWitnessFailure, connect_render_witness,
)


class RenderSentinelHalted(RuntimeError):
    """Fixed error categories, no database exceptions or request URLs."""


@dataclass(frozen=True)
class ShadowSentinelVerdict:
    status: str
    alert: bool
    age_seconds: float | None = None
    revision: int | None = None
    entry_allowed: bool = False
    live_authorization: bool = False
    mexc_orders_sent: bool = False


def render_sentinel_config(env):
    if (
        env.get("T100_EXECUTION_MODE") != "paper"
        or env.get("T100_LIVE_ORDER_API_ENABLED") != "false"
        or env.get("T100_LIVE_KILL_SWITCH") != "true"
        or env.get("T100_RENDER_SENTINEL_ENABLED") != "true"
        or env.get("MEXC_API_KEY") or env.get("MEXC_API_SECRET")
    ):
        raise RenderSentinelHalted("RENDER_SENTINEL_PAPER_NO_EXCHANGE_KEYS_REQUIRED")
    scope = env.get("T100_RENDER_SHADOW_SCOPE_SHA256", "")
    if re.fullmatch(r"[a-f0-9]{64}", scope) is None:
        raise RenderSentinelHalted("RENDER_SENTINEL_SCOPE_UNVERIFIED")
    dsn = env.get("T100_RENDER_SENTINEL_DATABASE_URL", "")
    if not (dsn.startswith("postgresql://") or dsn.startswith("postgres://")):
        raise RenderSentinelHalted("RENDER_SENTINEL_DB_READ_ROLE_REQUIRED")
    webhook = env.get("T100_RENDER_SENTINEL_DISCORD_WEBHOOK_URL", "")
    if re.fullmatch(r"https://discord\.com/api/webhooks/[0-9]+/[a-zA-Z0-9_-]+", webhook) is None:
        raise RenderSentinelHalted("RENDER_SENTINEL_DISCORD_REQUIRED")
    if webhook == env.get("DISCORD_TRADER_EVENTS_WEBHOOK_URL") or webhook == env.get(
        "T100_PILOT_DISCORD_WEBHOOK_URL"
    ):
        raise RenderSentinelHalted("RENDER_SENTINEL_DISTINCT_WEBHOOK_REQUIRED")
    try:
        stale = int(env.get("T100_RENDER_SENTINEL_STALE_SECONDS", "240"))
        interval = int(env.get("T100_RENDER_SENTINEL_POLL_SECONDS", "45"))
    except (ValueError, TypeError):
        raise RenderSentinelHalted("RENDER_SENTINEL_INTERVAL_INVALID") from None
    if not 120 <= stale <= 1800 or not 15 <= interval <= min(300, stale // 2):
        raise RenderSentinelHalted("RENDER_SENTINEL_INTERVAL_INVALID")
    return scope, dsn, webhook, stale, interval


async def inspect_witness(conn, scope: str, *, stale_seconds: int) -> ShadowSentinelVerdict:
    try:
        row = await RenderShadowWitness(conn, scope_sha256=scope).restore()
    except (ShadowWitnessFailure, Exception):
        return ShadowSentinelVerdict("SHADOW_WITNESS_DB_READ_FAILED", True)
    if row is None:
        return ShadowSentinelVerdict("SHADOW_WITNESS_MISSING", True)
    kw = {"age_seconds": row.age_seconds, "revision": row.revision}
    if row.kill_latched:
        return ShadowSentinelVerdict("SHADOW_WITNESS_KILL_LATCHED", True, **kw)
    if row.age_seconds > stale_seconds:
        return ShadowSentinelVerdict("SHADOW_WITNESS_STALE", True, **kw)
    if row.state != "OBSERVING_NON_AUTHORIZING":
        return ShadowSentinelVerdict("SHADOW_WITNESS_STATE_UNKNOWN", True, **kw)
    return ShadowSentinelVerdict("SHADOW_WITNESS_RECENT_NONAUTHORIZING", False, **kw)


async def run(*, environ=None, cycles: int | None = None,
              connect=connect_render_witness,
              notifier_factory=T100LiveOpsNotifier):
    env = os.environ if environ is None else environ
    scope, dsn, webhook, stale, interval = render_sentinel_config(env)
    if cycles is not None and (type(cycles) is not int or cycles < 1):
        raise RenderSentinelHalted("RENDER_SENTINEL_CYCLES_INVALID")
    notifier = notifier_factory(webhook, cooldown_seconds=60)
    previous = None
    count = 0
    try:
        while True:
            conn = None
            try:
                conn = await connect(dsn)
                result = await inspect_witness(conn, scope, stale_seconds=stale)
            except Exception:
                result = ShadowSentinelVerdict("SHADOW_WITNESS_CONNECT_FAILED", True)
            finally:
                if conn is not None:
                    await conn.close()
            # Report only predefined status codes. Never include DB/venue
            # data, account identities or private payloads in notifications.
            if result.alert and (previous != result.status or count % 10 == 0):
                await notifier.send(
                    OpsNotice(
                        key="t100-render-shadow-witness-"+result.status,
                        title="T100 RENDER SHADOW WITNESS ALERT",
                        description=result.status + " (nonauthorizing; operator review required)",
                        severity="critical",
                    ),
                    force=True,
                )
            previous = result.status
            count += 1
            if cycles is not None and count >= cycles:
                return result
            await asyncio.sleep(interval)
    finally:
        await notifier.close()


def main():
    parser = argparse.ArgumentParser(description="Independent GET-only DB Render shadow sentinel")
    parser.add_argument("--once", action="store_true")
    args = parser.parse_args()
    try:
        result = asyncio.run(run(cycles=1 if args.once else None))
        print("T100_RENDER_SHADOW_SENTINEL="+result.status)
        print("NO MEXC ORDERS. LIVE_AUTHORIZATION=FALSE")
        if result.alert:
            sys.exit(2)
    except Exception as exc:
        print("T100_RENDER_SHADOW_SENTINEL_HALTED="+
              (str(exc) if isinstance(exc, RenderSentinelHalted) else "SENTINEL_RUNTIME_FAILED"))
        print("NO MEXC ORDERS. LIVE_AUTHORIZATION=FALSE")
        sys.exit(2)


if __name__ == "__main__":
    main()
