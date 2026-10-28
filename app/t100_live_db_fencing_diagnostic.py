"""Operator-only PostgreSQL leader lease/fencing exercise on a test lease.

Uses two INDEPENDENT connections with the restricted T100 runtime role.
Never touches 't100-live-executor', other live leases, scanner tables, or
MEXC. A private per-run test lease is left expired; no DELETE permission.

The test executes real SQL for:
  - exclusive lease ownership and renewal
  - takeover after the real 15-second TTL expires
  - stale-token rejection (current and renew)
  - an expired same-holder reacquisition incrementing fencing token
  - verified release/final expiration

This is a DB implementation test, not certification of the whole live
executor's crash recovery or permission to send orders.
"""
from __future__ import annotations

import argparse
import asyncio
import getpass
import json
import os
import uuid
from typing import Any

from app.t100_live_db_inspect import EXPECTED_DATABASE
from app.t100_live_leader_fencing import (
    LeaderLeaseCoordinator,
    LeaderLeaseHalted,
    PostgresLeaderLeaseStore,
)
from app.t100_live_role_preflight import TARGET_ROLE
from app.t100_live_role_setup import independent_runtime_audit


class FencingDiagnosticHalted(RuntimeError):
    pass


LEASE_PREFIX = "t100-fencing-diagnostic-"
CONFIRMATION = "TEST RESTRICTED T100 LEADER FENCING"


async def _expect_stale(coordinator: LeaderLeaseCoordinator, lease: Any) -> None:
    try:
        await coordinator.require_current(lease)
    except LeaderLeaseHalted as exc:
        if str(exc) != "LEADER_FENCING_TOKEN_STALE":
            raise FencingDiagnosticHalted("UNEXPECTED_STALE_ASSERT_FAILURE") from None
    else:
        raise FencingDiagnosticHalted("STALE_LEASE_WRONGLY_CURRENT")
    try:
        await coordinator.renew(lease)
    except LeaderLeaseHalted as exc:
        if str(exc) != "LEADER_LEASE_LOST":
            raise FencingDiagnosticHalted("UNEXPECTED_STALE_RENEW_FAILURE") from None
    else:
        raise FencingDiagnosticHalted("STALE_LEASE_WRONGLY_RENEWED")


async def exercise_test_fencing(
    first: Any, second: Any, *,
    lease_name: str,
    sleep=asyncio.sleep,
) -> dict[str, Any]:
    if not lease_name.startswith(LEASE_PREFIX) or len(lease_name) > 100:
        raise FencingDiagnosticHalted("TEST_ONLY_LEASE_NAME_REQUIRED")

    owner_a = LeaderLeaseCoordinator(
        PostgresLeaderLeaseStore(first),
        lease_name=lease_name, ttl_seconds=15,
    )
    owner_b = LeaderLeaseCoordinator(
        PostgresLeaderLeaseStore(second),
        lease_name=lease_name, ttl_seconds=15,
    )
    evidence: dict[str, Any] = {
        "status": "FENCING_DIAGNOSTIC_INCOMPLETE",
        "role_name": TARGET_ROLE,
        "test_only_lease": True,
        "real_executor_lease_accessed": False,
        "live_authorization": False,
        "mexc_orders_sent": False,
    }
    try:
        initial = await first.fetchval(
            """SELECT count(*) FROM public.t100_live_executor_leases
               WHERE lease_name=$1""",
            lease_name,
        )
        if initial != 0:
            raise FencingDiagnosticHalted("TEST_LEASE_NAME_COLLISION")

        # Conn A acquires, conn B must be denied while the TTL is current.
        a = await owner_a.acquire("diagnostic-process-a")
        await owner_a.require_current(a)
        await owner_a.renew(a)
        try:
            await owner_b.acquire("diagnostic-process-b")
        except LeaderLeaseHalted as exc:
            if str(exc) != "LEADER_LEASE_NOT_ACQUIRED":
                raise FencingDiagnosticHalted(
                    "WRONG_SECOND_HOLDER_DENIAL"
                ) from None
            evidence["simultaneous_second_holder_denied"] = True
        else:
            raise FencingDiagnosticHalted("SECOND_HOLDER_ACCEPTED_EARLY")

        # Natural DB-clock expiry: no custom clock, no direct table mutation.
        await sleep(16.5)
        b = await owner_b.acquire("diagnostic-process-b")
        if b.fencing_token <= a.fencing_token:
            raise FencingDiagnosticHalted("TAKEOVER_TOKEN_DID_NOT_INCREASE")
        await owner_b.require_current(b)
        await _expect_stale(owner_a, a)
        evidence["real_ttl_takeover_verified"] = True
        evidence["stale_old_holder_rejected"] = True

        # The same holder reacquiring an expired lease must also get a
        # fresh fencing token, even with an identical holder ID.
        await owner_b.release(b)
        b2 = await owner_b.acquire("diagnostic-process-b")
        if b2.fencing_token <= b.fencing_token:
            raise FencingDiagnosticHalted("SAME_HOLDER_TOKEN_NOT_INCREMENTED")
        await owner_b.require_current(b2)
        await _expect_stale(owner_b, b)
        evidence["same_holder_expired_reacquire_verified"] = True
        evidence["same_holder_stale_token_rejected"] = True

        # A different holder can recover only once B has released.
        await owner_b.release(b2)
        a2 = await owner_a.acquire("diagnostic-process-a")
        if a2.fencing_token <= b2.fencing_token:
            raise FencingDiagnosticHalted("RECOVERY_TOKEN_NOT_INCREMENTED")
        await owner_a.require_current(a2)
        await _expect_stale(owner_b, b2)
        evidence["released_lease_recovered"] = True
        await owner_a.release(a2)

        row = await first.fetchrow(
            """SELECT lease_expires_at <= now() AS expired
               FROM public.t100_live_executor_leases
               WHERE lease_name=$1""",
            lease_name,
        )
        if row is None or row["expired"] is not True:
            raise FencingDiagnosticHalted("FINAL_TEST_LEASE_NOT_EXPIRED")
        evidence["final_test_lease_expired"] = True
        evidence["status"] = "RESTRICTED_POSTGRES_FENCING_DIAGNOSTIC_PASSED"
        return evidence
    finally:
        # Fail-closed emergency cleanup of only our random diagnostic lease.
        # The role cannot DELETE the row; an expired row is harmless and
        # preserves monotonic token history.
        try:
            await first.execute(
                """UPDATE public.t100_live_executor_leases
                   SET lease_expires_at=now()-interval '1 second',
                       updated_at=now()
                   WHERE lease_name=$1""",
                lease_name,
            )
        except Exception:
            # A forced disconnection lets natural TTL expire in 15 sec.
            # The caller reports a failed/uncertain test.
            if evidence.get("status") == (
                "RESTRICTED_POSTGRES_FENCING_DIAGNOSTIC_PASSED"
            ):
                raise FencingDiagnosticHalted(
                    "FINAL_TEST_LEASE_CLEANUP_UNVERIFIED"
                ) from None


async def _main() -> None:
    if os.getenv("T100_EXECUTION_MODE", "paper").strip().lower() != "paper":
        raise FencingDiagnosticHalted("PAPER_MODE_REQUIRED")
    typed = input(f"Type '{CONFIRMATION}' to run isolated DB test: ")
    if typed.strip() != CONFIRMATION:
        raise FencingDiagnosticHalted("OPERATOR_CONFIRMATION_MISSING")
    password = getpass.getpass("Restricted T100 runtime DB password (hidden): ")
    if not password:
        raise FencingDiagnosticHalted("RUNTIME_PASSWORD_REQUIRED")
    owner_url = getpass.getpass(
        "Existing Render PostgreSQL External URL for host/DB only (hidden): "
    )
    if not owner_url:
        raise FencingDiagnosticHalted("DATABASE_URL_REQUIRED")
    import asyncpg
    conns = []
    try:
        for _ in range(2):
            connection = await asyncpg.connect(
                dsn=owner_url,
                user=TARGET_ROLE,
                password=password,
                ssl="require",
                timeout=10,
                command_timeout=20,
            )
            conns.append(connection)
        # Both actual connections must be the restricted role.
        for conn in conns:
            if await conn.fetchval("SELECT current_database()") != EXPECTED_DATABASE:
                raise FencingDiagnosticHalted("WRONG_DATABASE_IDENTITY")
            if await conn.fetchval("SELECT current_user") != TARGET_ROLE:
                raise FencingDiagnosticHalted("WRONG_RUNTIME_ROLE")

        initial_audit = await independent_runtime_audit(conns[0])
        if initial_audit.get("status") != (
            "RESTRICTED_T100_RUNTIME_ROLE_VERIFIED"
        ):
            raise FencingDiagnosticHalted(
                "RUNTIME_ROLE_EFFECTIVE_PRIVILEGES_UNVERIFIED"
            )
        report = await exercise_test_fencing(
            conns[0], conns[1],
            lease_name=LEASE_PREFIX + uuid.uuid4().hex,
        )
        print(json.dumps(report, indent=2, sort_keys=True))
        print("TEST LEASE ONLY. NO MEXC API. LIVE TRADING DISABLED.")
    except FencingDiagnosticHalted:
        raise
    except Exception as exc:
        # Keep errors secret-safe: underlying DB error messages and DSN
        # details must never appear in operator-shared output.
        raise FencingDiagnosticHalted(
            "FENCING_DB_TEST_FAILED:" + type(exc).__name__
        ) from None
    finally:
        for conn in reversed(conns):
            try:
                await conn.close()
            except Exception:
                pass


def cli() -> None:
    parser = argparse.ArgumentParser(
        description="Isolated real-Postgres T100 leader fencing diagnostic"
    )
    parser.parse_args()
    try:
        asyncio.run(_main())
    except FencingDiagnosticHalted as exc:
        print(json.dumps({
            "status": "FENCING_DIAGNOSTIC_BLOCKED",
            "reason": str(exc),
            "live_authorization": False,
        }, sort_keys=True))
        raise SystemExit(2) from None


if __name__ == "__main__":
    cli()
