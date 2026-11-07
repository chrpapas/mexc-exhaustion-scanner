"""Opt-in Render sentinel with PostgreSQL-committed, restart-safe alert outbox.

This is a *separate read-only exchange observer* in the security sense:
no MEXC API keys/client, no order/cancel/stop transport. It does perform
limited Postgres INSERT/UPDATE to persist and acknowledge Discord alarms.
Migration 032 and least-privilege DB role must exist BEFORE deployment.
"""
from __future__ import annotations

import argparse
import asyncio
import logging
import os
import sys
from dataclasses import dataclass
from typing import Any

from app.t100_live_ops_notifier import OpsNotice, T100LiveOpsNotifier
from app.t100_render_shadow_witness import connect_render_witness
from app.t100_render_shadow_sentinel import (
    RenderSentinelHalted, inspect_witness, render_sentinel_config,
)
from app.t100_render_shadow_alert_outbox import (
    RenderAlertOutboxFailure, RenderShadowAlertOutbox,
)

LOGGER = logging.getLogger(__name__)


@dataclass(frozen=True)
class DurableShadowSentinelCycle:
    status: str
    alarm: bool
    pending: int
    delivered_this_cycle: int
    entry_allowed: bool = False
    live_authorization: bool = False
    mexc_orders_sent: bool = False


def durable_config(env):
    if env.get("T100_RENDER_SENTINEL_DURABLE_OUTBOX_ENABLED") != "true":
        raise RenderSentinelHalted("RENDER_SENTINEL_DURABLE_OUTBOX_REQUIRED")
    return render_sentinel_config(env)


async def run(*, environ=None, cycles: int | None = None,
              connect=connect_render_witness,
              notifier_factory=T100LiveOpsNotifier) -> DurableShadowSentinelCycle:
    """Persist every new alert *before* Discord delivery.

    PostgreSQL AFTER INSERT/UPDATE trigger on sentinel_state inserts new
    outbox records atomically; delivered records survive restart. Failed
    sends stay pending and are retried. A send-ACK-before-commit crash may
    produce duplicate delivery: at-least-once, NOT exactly-once.
    """
    env = os.environ if environ is None else environ
    scope, dsn, webhook, stale, interval = durable_config(env)
    if cycles is not None and (type(cycles) is not int or cycles < 1):
        raise RenderSentinelHalted("RENDER_SENTINEL_CYCLES_INVALID")
    notifier = notifier_factory(webhook, cooldown_seconds=0)
    conn: Any = None
    count = 0
    try:
        conn = await connect(dsn)
        outbox = RenderShadowAlertOutbox(conn, scope_sha256=scope)
        await outbox.acquire_singleton()
        while True:
            verdict = await inspect_witness(conn, scope, stale_seconds=stale)
            await outbox.observe(code=verdict.status, alert=verdict.alert)
            delivered = 0
            for item in await outbox.pending(limit=10):
                # Attempt count is persisted *before* network call. A lost
                # ACK leaves the row pending, never silently acknowledged.
                await outbox.mark_attempt(item.seq)
                try:
                    sent = await asyncio.wait_for(
                        notifier.send(
                            OpsNotice(
                                key="t100-render-witness-"+item.code,
                                title="T100 RENDER SHADOW WITNESS ALERT",
                                description=(
                                    item.code +
                                    " (durable nonauthorizing alarm; operator review required)"
                                ),
                                severity="critical",
                            ),
                            force=True,
                        ),
                        timeout=8.0,
                    )
                except Exception:
                    # Never log the notifier exception: it may contain a
                    # private Discord URL. Keep the committed row pending.
                    sent = False
                if sent is not True:
                    break
                await outbox.mark_delivered(item.seq)
                delivered += 1
            pending = await outbox.count_pending()
            report = DurableShadowSentinelCycle(
                status=verdict.status, alarm=verdict.alert,
                pending=pending, delivered_this_cycle=delivered,
            )
            LOGGER.info(
                "T100 durable Render sentinel status=%s pending=%s "
                "delivered=%s entry_allowed=false live_authorization=false",
                report.status, report.pending, report.delivered_this_cycle,
            )
            count += 1
            if cycles is not None and count >= cycles:
                return report
            await asyncio.sleep(interval)
    except (RenderAlertOutboxFailure, RenderSentinelHalted):
        raise
    except Exception:
        # The sentinel cannot guarantee delivery while Postgres is down.
        # Halt instead of claiming a healthy observation or silently
        # replacing the durable channel with an untracked one.
        raise RenderSentinelHalted("RENDER_SENTINEL_RUNTIME_OR_DB_FAILED") from None
    finally:
        if conn is not None:
            await conn.close()
        await notifier.close()


def main():
    parser = argparse.ArgumentParser(
        description="T100 separate Render PostgreSQL/Discord durable alert sentinel"
    )
    parser.add_argument("--once", action="store_true")
    args = parser.parse_args()
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
    )
    try:
        result = asyncio.run(run(cycles=1 if args.once else None))
        print("T100_RENDER_SENTINEL="+result.status)
        print("T100_RENDER_SENTINEL_PENDING="+str(result.pending))
        print("NO MEXC ORDERS. LIVE_AUTHORIZATION=FALSE")
        if result.alarm or result.pending:
            sys.exit(2)
    except Exception as exc:
        if isinstance(exc, (RenderAlertOutboxFailure, RenderSentinelHalted)):
            status = str(exc)
        else:
            status = "RENDER_SENTINEL_UNEXPECTED_FAILURE"
        print("T100_RENDER_SENTINEL_HALTED="+status)
        print("NO MEXC ORDERS. LIVE_AUTHORIZATION=FALSE")
        sys.exit(2)


if __name__ == "__main__":
    main()
