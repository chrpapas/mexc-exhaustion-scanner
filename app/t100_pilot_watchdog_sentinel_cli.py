"""Stand-alone pilot watchdog death-alert sentinel; NO exchange access.

Must be run in a process distinct from the watched MEXC GET-only observer.
It reads the watched SQLite journal only, writes a separate local alert outbox
and optionally posts redacted WATCH_* codes to a SEPARATE Discord webhook.
It does not read or need MEXC credentials and cannot submit orders.
Not deployed; no external process manager or peer monitor is supplied.
"""
from __future__ import annotations

import argparse
import asyncio
import os
import re
import sys
from pathlib import Path

from app.t100_pilot_watchdog_sentinel import PilotLivenessSentinel, SentinelHalted


class SentinelServiceHalted(RuntimeError):
    """Redacted CLI error category."""


def parse_config(env, *, cycles):
    if (
        env.get("T100_EXECUTION_MODE") != "paper"
        or env.get("T100_LIVE_ORDER_API_ENABLED") != "false"
        or env.get("T100_LIVE_KILL_SWITCH") != "true"
    ):
        raise SentinelServiceHalted("SENTINEL_PAPER_KILL_REQUIRED")
    scope = env.get("T100_PILOT_SENTINEL_EXPECTED_SCOPE_SHA256", "")
    if re.fullmatch(r"[a-f0-9]{64}", scope) is None:
        raise SentinelServiceHalted("SENTINEL_EXPECTED_SCOPE_MISSING")
    watched = Path(env.get("T100_PILOT_SENTINEL_WATCHDOG_JOURNAL", ""))
    outbox = Path(env.get("T100_PILOT_SENTINEL_ALERT_JOURNAL", ""))
    if not watched.is_absolute() or not outbox.is_absolute() or watched == outbox:
        raise SentinelServiceHalted("SENTINEL_SEPARATE_ABSOLUTE_JOURNALS_REQUIRED")
    webhook = env.get("T100_PILOT_SENTINEL_DISCORD_WEBHOOK_URL", "")
    if (
        re.fullmatch(r"https://discord\.com/api/webhooks/[0-9]+/[A-Za-z0-9_-]+", webhook) is None
        or webhook == env.get("T100_PILOT_DISCORD_WEBHOOK_URL")
        or webhook == env.get("DISCORD_TRADER_EVENTS_WEBHOOK_URL")
    ):
        raise SentinelServiceHalted("SENTINEL_INDEPENDENT_DISCORD_REQUIRED")
    if cycles is not None and (type(cycles) is not int or cycles < 1):
        raise SentinelServiceHalted("SENTINEL_INVALID_CYCLES")
    return watched, outbox, scope, webhook


async def run_sentinel(*, environ=None, cycles=1):
    from app.t100_live_ops_notifier import OpsNotice, T100LiveOpsNotifier

    env = os.environ if environ is None else environ
    watched, outbox, scope, webhook = parse_config(env, cycles=cycles)
    notifier = T100LiveOpsNotifier(webhook, cooldown_seconds=0)
    async def send(code):
        if re.fullmatch(r"WATCH_(?:HEARTBEAT|SENTINEL)_[A-Z0-9_]{1,90}", code) is None:
            return False
        return await notifier.send(
            OpsNotice(
                key="t100-watchdog-death-" + code,
                title="T100 pilot watchdog liveness alarm",
                description=code,
                severity="critical",
            ),
            force=True,
        )
    try:
        with PilotLivenessSentinel(
            watched_journal=watched,
            outbox_journal=outbox,
            expected_scope_sha256=scope,
            notifier=send,
            stale_after_ms=30000,
            poll_seconds=5.0,
            notify_timeout_seconds=8.0,
        ) as sentinel:
            return await sentinel.run(max_cycles=cycles)
    finally:
        await notifier.close()


def main():
    parser = argparse.ArgumentParser(description="Read-only independent pilot watchdog sentinel")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--cycles", default=1, type=int)
    mode.add_argument("--continuous", action="store_true")
    args = parser.parse_args()
    count = None if args.continuous else args.cycles
    try:
        result = asyncio.run(run_sentinel(cycles=count))
    except Exception as exc:
        print("T100 SENTINEL HALTED:",
              str(exc) if isinstance(exc, (SentinelHalted, SentinelServiceHalted))
              else "SENTINEL_RUNTIME_FAILED")
        sys.exit(2)
    print("T100 SENTINEL STATE:", result.status)
    print("READ-ONLY WATCHDOG LIVENESS. NO MEXC ORDERS.")
    if result.alarm_latched or result.notifications_pending:
        sys.exit(2)


if __name__ == "__main__":
    main()
