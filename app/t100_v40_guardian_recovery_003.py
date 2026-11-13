"""Single audited recovery from the third V40 GET-only inventory incident.

This is NOT an incident-latch bypass or UPDATE. The three older incident
histories, critical latches and delivered Discord alerts remain immutable.
A new (fourth) monitored generation can be initialized ONCE only after
checking all historical critical events and repeating fresh signed account
and independent durable-ledger FLAT observations.

An unknown position/stop, missing delivery, stale observation, DB failure,
or any other incident code fails closed. A critical incident in this final
generation is sticky: NEVER create a fifth.
"""
from __future__ import annotations

import hashlib
from typing import Any, Awaitable, Callable, Mapping

from app.t100_live_guardian_incident_outbox import GuardianIncidentOutbox
from app.t100_v40_canary_account_guardian import V40CanaryGuardianHeartbeat
from app.t100_v40_guardian_reviewed_generation import (
    AUDIT_STATUS,GOOD_STATUS,HISTORIC_CRITICAL,LEGACY_SCOPE,
    REVIEWED_SCOPE,RECOVERY_SCOPE_002,V40GuardianReviewedGenerationHalted,
)

RECOVERY_SCOPE_003 = hashlib.sha256(
    b"T100_V40_DEDICATED_FIRST_CANARY_GUARDIAN_REVIEWED_2026_11_13_003"
).hexdigest()
ACTIVE_REVIEWED_SCOPE = RECOVERY_SCOPE_003
_PREVIOUS = (LEGACY_SCOPE, REVIEWED_SCOPE, RECOVERY_SCOPE_002)
_REQUIRED_INCIDENT = HISTORIC_CRITICAL

async def select_third_reviewed_scope(
    conn: Any, *, env: Mapping[str,str],
    observe: Callable[[Mapping[str,str]], Awaitable[V40CanaryGuardianHeartbeat]],
    pause: Callable[[float], Awaitable[Any]],
) -> tuple[str,str]:
    """Already under the exclusive, account-wide guardian SQL advisory lock."""
    try:
        existing = await conn.fetchrow(
            """SELECT last_status,last_alert,critical_latched
               FROM public.t100_guardian_incident_state
               WHERE scope_sha256=$1""",
            RECOVERY_SCOPE_003,
        )
        if existing is not None:
            if existing["last_status"] not in (
                AUDIT_STATUS, GOOD_STATUS, HISTORIC_CRITICAL,
                "OBSERVER_V40_CANARY_NONFLAT_UNKNOWN_OWNER",
                "OBSERVER_V40_CANARY_OWNED_CROSS_NATIVE_MARKET_SL",
            ):
                raise V40GuardianReviewedGenerationHalted(
                    "V40_AUDITED_ROLLOVER_REVIEWED_STATE_INVALID"
                )
            # NEVER insert a second event, relabel, or clear a 003 latch.
            return RECOVERY_SCOPE_003,"EXISTING_RECOVERY_003_SCOPE"

        for scope in _PREVIOUS:
            state = await conn.fetchrow(
                """SELECT last_status,last_alert,critical_latched,revision,
                          observed_at >= clock_timestamp()
                          - interval '120 seconds' AS recently_healthy
                   FROM public.t100_guardian_incident_state
                   WHERE scope_sha256=$1""", scope,
            )
            if (
                state is None
                or state["last_status"] != GOOD_STATUS
                or state["last_alert"] is not False
                or state["critical_latched"] is not True
                or type(state["revision"]) is not int or state["revision"] < 3
                or scope == RECOVERY_SCOPE_002 and state["recently_healthy"] is not True
            ):
                raise V40GuardianReviewedGenerationHalted(
                    "V40_RECOVERY_003_PREDECESSOR_STATE_NOT_RECOVERED"
                )
            counts = await conn.fetchrow(
                """SELECT
                     COUNT(*) FILTER (WHERE severity='critical')::int AS critical_total,
                     COUNT(*) FILTER (WHERE severity='critical' AND code<>$2)::int
                       AS forbidden_critical,
                     COUNT(*) FILTER (WHERE delivered_at IS NULL)::int AS undelivered
                   FROM public.t100_guardian_incident_outbox
                   WHERE scope_sha256=$1""",
                scope, _REQUIRED_INCIDENT,
            )
            if (
                counts is None
                or type(counts["critical_total"]) is not int
                or counts["critical_total"] != 1
                or counts["forbidden_critical"] != 0
                or counts["undelivered"] != 0
            ):
                raise V40GuardianReviewedGenerationHalted(
                    "V40_RECOVERY_003_PREDECESSOR_ALERT_NOT_REVIEWED"
                )

        # Independent account/ledger snapshots, not cached heartbeats.
        for i in range(2):
            try:
                snapshot = await observe(env)
            except Exception:
                raise V40GuardianReviewedGenerationHalted(
                    "V40_RECOVERY_003_FRESH_FLAT_UNVERIFIED"
                ) from None
            if (
                type(snapshot) is not V40CanaryGuardianHeartbeat
                or snapshot.status !=
                   "V40_GUARDIAN_FLAT_ACCOUNT_DOUBLE_GET_STANDBY_NONAUTHORIZING"
                or snapshot.monitored_account_flat is not True
                or snapshot.inventory_two_signed_get_bookends is not True
                or snapshot.hedge_mode_verified is not True
                or snapshot.ledger_provenance_verified is not True
                or snapshot.incident_alert is not False
                or snapshot.mexc_posts != 0
                or snapshot.database_writes != 0
            ):
                raise V40GuardianReviewedGenerationHalted(
                    "V40_RECOVERY_003_FRESH_FLAT_UNVERIFIED"
                )
            if i == 0:
                await pause(3.0)
        new_scope = GuardianIncidentOutbox(conn, scope_sha256=RECOVERY_SCOPE_003)
        first = await new_scope.observe(status=AUDIT_STATUS,alert=False)
        if first.critical_latched is not False or first.revision != 1:
            raise V40GuardianReviewedGenerationHalted(
                "V40_RECOVERY_003_NEW_SCOPE_NOT_CLEAN"
            )
        return RECOVERY_SCOPE_003,"NEW_RECOVERY_003_AFTER_THREE_HISTORIC_ALERTS_AND_DOUBLE_GET"
    except V40GuardianReviewedGenerationHalted:
        raise
    except Exception:
        raise V40GuardianReviewedGenerationHalted(
            "V40_RECOVERY_003_DATABASE_REVIEW_UNAVAILABLE"
        ) from None
