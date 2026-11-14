"""One-time audited V40 guardian incident-preserving recovery for 2026-11-13.

Operator-reviewed incident evidence:
  17:42:05, 19:09:01, 21:58:40 UTC on 2026-11-13: each guardian
  failure was observed at the HEDGE_MODE_SIGNED_GET step, with a
  restored FLAT/hedge/ledger double-GET on restart. The durable 003 outbox
  contains precisely THREE delivered generic observer-class criticals.
  The lower-level transport exception wasn't categorized at that time, so
  this is NOT a proof of HTTP status or a waiver of any future incident.

This SELECT-verified transition makes ONE new safety generation; it never
clears or relabels any older critical latch. Requires four exactly audited
previous scopes, positive delivery, current 003 healthy FLAT, and TWO
fresh independent account/immutable-ledger inspections before first
unlatched 004 status. The singleton is held by the guardian caller.

An unexpected position, missing stop, fresh GET failure, unacknowledged
alert or ledger inconsistency still prevents recovery. A future critical in
004 is sticky and MUST NOT trigger automatic 005.
No trading authorization or exchange POST is created here.
"""
from __future__ import annotations

from hashlib import sha256
from typing import Any, Awaitable, Callable, Mapping

from app.t100_live_guardian_incident_outbox import GuardianIncidentOutbox
from app.t100_v40_canary_account_guardian import V40CanaryGuardianHeartbeat
from app.t100_v40_guardian_reviewed_generation import (
    AUDIT_STATUS, GOOD_STATUS, HISTORIC_CRITICAL,
    LEGACY_SCOPE, REVIEWED_SCOPE, RECOVERY_SCOPE_002,
)
from app.t100_v40_guardian_recovery_003 import RECOVERY_SCOPE_003

RECOVERY_SCOPE_004=sha256(
    b"T100_V40_DEDICATED_FIRST_CANARY_GUARDIAN_AUDITED_2026_11_14_004"
).hexdigest()
ACTIVE_REVIEWED_SCOPE=RECOVERY_SCOPE_004

_OLD_SCOPES=(
    (LEGACY_SCOPE,1),
    (REVIEWED_SCOPE,1),
    (RECOVERY_SCOPE_002,1),
    (RECOVERY_SCOPE_003,3),
)


class V40GuardianRecovery004Halted(RuntimeError):
    """Fixed nonsecret reason; never attach SQL/exchange error text."""


async def select_fourth_reviewed_scope(
    conn:Any, *,
    env:Mapping[str,str],
    observe:Callable[[Mapping[str,str]],Awaitable[V40CanaryGuardianHeartbeat]],
    pause:Callable[[float],Awaitable[Any]],
)->tuple[str,str]:
    """Called ONLY by the sole guardian under its exclusive DB advisory lock.

    Must not be called as an external database migration or latch bypass.
    An existing 004, even if latched, is never updated or reinitialized.
    """
    def fail(code:str)->None:
        raise V40GuardianRecovery004Halted(code)
    try:
        if conn is None or not callable(getattr(conn,"fetchrow",None)):
            fail("V40_RECOVERY_004_DB_CONNECTION_INVALID")
        existing=await conn.fetchrow(
            """SELECT last_status,last_alert,critical_latched
               FROM public.t100_guardian_incident_state
               WHERE scope_sha256=$1""",
            RECOVERY_SCOPE_004,
        )
        if existing is not None:
            if (
                existing["last_status"] not in (
                    AUDIT_STATUS,GOOD_STATUS,HISTORIC_CRITICAL,
                    "OBSERVER_V40_CANARY_NONFLAT_UNKNOWN_OWNER",
                    "OBSERVER_V40_CANARY_OWNED_CROSS_NATIVE_MARKET_SL",
                )
                or type(existing["last_alert"]) is not bool
                or type(existing["critical_latched"]) is not bool
            ):
                fail("V40_RECOVERY_004_EXISTING_SCOPE_STATE_INVALID")
            return RECOVERY_SCOPE_004,"EXISTING_RECOVERY_004_SCOPE_STICKY"

        for scope,expected_critical in _OLD_SCOPES:
            row=await conn.fetchrow(
                """SELECT last_status,last_alert,critical_latched,revision,
                    observed_at >= clock_timestamp()-interval '120 seconds'
                        AS recently_healthy
                   FROM public.t100_guardian_incident_state
                   WHERE scope_sha256=$1""",scope,
            )
            if (
                row is None
                or row["last_status"]!=GOOD_STATUS
                or row["last_alert"] is not False
                or row["critical_latched"] is not True
                or type(row["revision"]) is not int or row["revision"]<3
                or (
                    scope==RECOVERY_SCOPE_003
                    and row["recently_healthy"] is not True
                )
            ):
                fail("V40_RECOVERY_004_PREDECESSOR_STATE_NOT_REVIEWED")
            counts=await conn.fetchrow(
                """SELECT
                    COUNT(*) FILTER (WHERE severity='critical')::int AS total,
                    COUNT(*) FILTER (
                        WHERE severity='critical' AND code=$2
                    )::int AS observer_class,
                    COUNT(*) FILTER (WHERE severity='critical' AND code<>$2)::int
                        AS other_critical,
                    COUNT(*) FILTER (WHERE delivered_at IS NULL)::int AS pending
                   FROM public.t100_guardian_incident_outbox
                   WHERE scope_sha256=$1""",
                scope,HISTORIC_CRITICAL,
            )
            if (
                counts is None
                or type(counts["total"]) is not int
                or counts["total"]!=expected_critical
                or counts["observer_class"]!=expected_critical
                or counts["other_critical"]!=0
                or counts["pending"]!=0
            ):
                fail("V40_RECOVERY_004_PREDECESSOR_INCIDENT_MISMATCH")

        # No cached guardian heartbeat, no caller-supplied "ready" boolean.
        # Both full observers must independently revalidate exact exchange
        # hedge mode, account inventory/current stops/open orders, and
        # immutable owned-position ledger (including empty inventory).
        for i in range(2):
            try:
                snap=await observe(env)
            except Exception:
                fail("V40_RECOVERY_004_FRESH_ACCOUNT_GET_OR_LEDGER_UNVERIFIED")
            if (
                type(snap) is not V40CanaryGuardianHeartbeat
                or snap.status!=
                    "V40_GUARDIAN_FLAT_ACCOUNT_DOUBLE_GET_STANDBY_NONAUTHORIZING"
                or snap.monitored_account_flat is not True
                or snap.inventory_two_signed_get_bookends is not True
                or snap.hedge_mode_verified is not True
                or snap.ledger_provenance_verified is not True
                or snap.incident_alert is not False
                or snap.mexc_posts !=0 or snap.database_writes !=0
            ):
                fail("V40_RECOVERY_004_FRESH_ACCOUNT_GET_OR_LEDGER_UNVERIFIED")
            if i==0:
                await pause(3.0)

        outbox=GuardianIncidentOutbox(conn,scope_sha256=RECOVERY_SCOPE_004)
        state=await outbox.observe(status=AUDIT_STATUS,alert=False)
        if state.critical_latched is not False or state.revision!=1:
            fail("V40_RECOVERY_004_NEW_SCOPE_NOT_CLEAN")
        return (
            RECOVERY_SCOPE_004,
            "NEW_RECOVERY_004_AFTER_FOUR_GENERATIONS_AND_DOUBLE_SIGNED_GET",
        )
    except V40GuardianRecovery004Halted:
        raise
    except Exception:
        raise V40GuardianRecovery004Halted(
            "V40_RECOVERY_004_DATABASE_OR_OBSERVER_UNAVAILABLE"
        ) from None
