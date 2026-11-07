"""T100-owned guardian observation-only worker for a SEPARATE Render service.

This module never imports a trading gateway or stop writer. Its only
exchange path is GetOnlyMexcTransport via run_with_network (signed GET);
DB access is in a SELECT-only, READ ONLY transaction.

Disabled by default. No auto-enrollment, migration, order placement,
position adoption, or kill-latch reset. Intended to be deployed only after
an independently provisioned restricted key/role and reviewed account pin.
A successful observation is NOT permission to trade.
"""
from __future__ import annotations

import argparse
import asyncio
from dataclasses import dataclass
import json
import os
import sys
from typing import Any, Awaitable, Callable, Mapping

from app.t100_live_guardian_readonly_once import (
    GuardianOnceHalted,
    ReadOnlyCycleSummary,
    run_with_network,
    validate_once_config,
)


@dataclass(frozen=True)
class GuardianWorkerConfig:
    interval_seconds: int = 90
    maximum_failures: int = 3

    @classmethod
    def from_env(cls, env: Mapping[str, str]) -> "GuardianWorkerConfig":
        if env.get("T100_GUARDIAN_WORKER_ENABLED") != "true":
            raise GuardianOnceHalted("GUARDIAN_WORKER_NOT_EXPLICITLY_ENABLED")
        try:
            interval = int(env.get("T100_GUARDIAN_INTERVAL_SECONDS", "90"))
            failures = int(env.get("T100_GUARDIAN_MAX_FAILURES", "3"))
        except (ValueError, TypeError):
            raise GuardianOnceHalted("GUARDIAN_WORKER_LIMITS_INVALID") from None
        if (not 30 <= interval <= 3600 or
                not 1 <= failures <= 5 or
                type(interval) is not int or type(failures) is not int):
            raise GuardianOnceHalted("GUARDIAN_WORKER_LIMITS_OUT_OF_RANGE")
        # Account, position, Keychain-equivalent credentials, TLS CA, and
        # paper-mode kill switch must all be present before any connection.
        validate_once_config(env)
        return cls(interval, failures)


@dataclass(frozen=True)
class GuardianHeartbeat:
    cycle: int
    status: str
    result: str
    alert: bool
    managed_position_observed: bool = False
    native_market_stop_observed: bool = False
    foreign_positions_seen: bool = False
    consecutive_failures: int = 0
    entry_allowed: bool = False
    emergency_close_allowed: bool = False
    live_authorization: bool = False
    mexc_orders_sent: int = 0
    database_mutations: int = 0


def _classify(report: Any, cycle: int, failures: int) -> GuardianHeartbeat:
    """Positive managed-protection observation is distinct from live readiness."""
    if type(report) is not ReadOnlyCycleSummary:
        return GuardianHeartbeat(cycle, "OBSERVER_UNVERIFIED_REPORT",
                                 "UNVERIFIED", True,
                                 consecutive_failures=failures)
    protected = (
        report.status == "OWNED_NATIVE_MARKET_STOP_CURRENTLY_OBSERVED"
        and report.managed_position_observed is True
        and report.native_market_stop_verified is True
        and report.alert is False
    )
    closed = (
        report.status == "OWNED_POSITION_CLOSED_IN_EXCHANGE_HISTORY"
        and report.managed_position_observed is False
        and report.native_market_stop_verified is False
        and report.alert is False
    )
    if protected:
        kind, status, alert = "PROTECTION_OBSERVED", "OBSERVER_CYCLE_OK", False
    elif closed:
        kind, status, alert = "OWNED_CLOSURE_OBSERVED", "OBSERVER_CYCLE_INACTIVE", False
    elif report.status == "NO_MANAGED_CHECKPOINT_NO_EXCHANGE_ACTION":
        # The absence of a checkpoint cannot mean protection is verified.
        kind, status, alert = "UNENROLLED_OR_IDLE", "OBSERVER_UNENROLLED", True
    else:
        kind, status, alert = "UNCERTIFIED", "OBSERVER_SAFETY_ALERT", True
    return GuardianHeartbeat(
        cycle=cycle,
        status=status,
        result=kind,
        alert=alert,
        managed_position_observed=protected,
        native_market_stop_observed=protected,
        foreign_positions_seen=(type(report.foreign_position_count) is int and
                                report.foreign_position_count > 0),
        consecutive_failures=failures,
    )


async def run_worker(
    env: Mapping[str, str],
    *,
    observe: Callable[[Mapping[str, str]], Awaitable[ReadOnlyCycleSummary]] = run_with_network,
    pause: Callable[[float], Awaitable[Any]] = asyncio.sleep,
    emit: Callable[[str], Any] = print,
    max_cycles: int = 0,
) -> int:
    """All log lines are whitelisted status fields; never print exceptions.

    max_cycles=0 runs forever; a finite value is for offline tests/explicit
    operator-controlled one-cycle validation, not a production stop timer.
    """
    config = GuardianWorkerConfig.from_env(env)
    if type(max_cycles) is not int or max_cycles < 0:
        raise GuardianOnceHalted("GUARDIAN_MAX_CYCLES_INVALID")
    consecutive = 0
    cycle = 0
    while True:
        cycle += 1
        try:
            report = await observe(env)
            candidate = _classify(report, cycle, consecutive)
            if candidate.alert:
                consecutive += 1
            else:
                consecutive = 0
            heartbeat = _classify(report, cycle, consecutive)
        except Exception:
            consecutive += 1
            heartbeat = GuardianHeartbeat(
                cycle=cycle,
                status="OBSERVER_SIGNED_GET_OR_DB_UNAVAILABLE",
                result="UNCERTIFIED",
                alert=True,
                consecutive_failures=consecutive,
            )
        emit(json.dumps(heartbeat.__dict__, sort_keys=True))
        if consecutive >= config.maximum_failures:
            emit(json.dumps({
                "status": "OBSERVER_FAIL_CLOSED_FAILURE_LIMIT",
                "consecutive_failures": consecutive,
                "entry_allowed": False,
                "emergency_close_allowed": False,
                "live_authorization": False,
                "mexc_orders_sent": 0,
                "database_mutations": 0,
            }, sort_keys=True))
            return 2
        if max_cycles and cycle >= max_cycles:
            return 1 if heartbeat.alert else 0
        await pause(config.interval_seconds)


def cli() -> int:
    parser = argparse.ArgumentParser(description="T100 GET-only guardian worker")
    parser.add_argument(
        "--run", action="store_true",
        help="Explicit real read-only observations (requires isolated account pin)",
    )
    parser.add_argument(
        "--once", action="store_true",
        help="Execute one observation and exit; only valid with --run",
    )
    args = parser.parse_args()
    if not args.run:
        print(json.dumps({
            "status": "GUARDIAN_WORKER_NOT_STARTED",
            "entry_allowed": False,
            "live_authorization": False,
            "mexc_orders_sent": 0,
        }, sort_keys=True))
        return 0
    try:
        return asyncio.run(run_worker(os.environ, max_cycles=1 if args.once else 0))
    except Exception as exc:
        # Fail closed before connecting if any pin, DB role, CA or execution
        # kill-switch is absent. Never log exception or supplied secret/DSN.
        print(json.dumps({
            "status": "GUARDIAN_WORKER_CONFIG_HALTED",
            "error_class": type(exc).__name__,
            "entry_allowed": False,
            "emergency_close_allowed": False,
            "live_authorization": False,
            "mexc_orders_sent": 0,
        }, sort_keys=True))
        return 2


if __name__ == "__main__":
    raise SystemExit(cli())
