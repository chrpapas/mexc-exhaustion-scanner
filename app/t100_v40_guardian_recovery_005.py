"""Explicit one-time incident-PRESERVING guardian recovery after 2026-11-15.

NOT a kill-switch reset, DB UPDATE of an old critical, MEXC POST, or
automatic rollover. Operator must explicitly opt in at startup. A new,
unique guardian generation may be initialized once, and ONLY when:
* all 5 old scopes exist with the exact previously reviewed critical
  observer-only history, zero undelivered alerts, and sticky old latches;
* all THREE 004 criticals exactly match the independently retrieved
  Render guardian timestamps:
  2026-11-15 12:14:31Z inventory before stops,
  2026-11-16 13:15:36Z inventory read/format failure,
  2026-11-17 11:49:40Z recovery bookend read failure;
* 004 has a fresh healthy double-GET FLAT observation and no new alert;
* two more independent signed hedge/positions/stops/order/ledger bookends
  on the same isolated exchange account both prove FLAT;
* this guardian has exclusive cross-scope PostgreSQL advisory lock.

Any mismatch, additional critical, protected/nonflat position, GET error,
history ambiguity or alert failure halts WITHOUT initializing 005. Existing
005 is STICKY: never reinitializes it, including after a new critical.
Historical event rows and state remain immutable and intact.

The worker and future entry gates still need separate exchange margin/SL
protection certification; new scope alone does not authorize live trading.
"""
from __future__ import annotations

from hashlib import sha256
from typing import Any, Awaitable, Callable, Mapping

from app.t100_live_guardian_incident_outbox import GuardianIncidentOutbox
from app.t100_v40_canary_account_guardian import V40CanaryGuardianHeartbeat
from app.t100_v40_guardian_recovery_004 import (
    RECOVERY_SCOPE_004,_OLD_SCOPES as _PRIOR_FOUR,
)
from app.t100_v40_guardian_reviewed_generation import (
    AUDIT_STATUS,GOOD_STATUS,HISTORIC_CRITICAL,
)

RECOVERY_SCOPE_005=sha256(
    b"T100_V40_DEDICATED_GUARDIAN_OPERATOR_REVIEWED_2026_11_15_005"
).hexdigest()
ACTIVE_REVIEWED_SCOPE=RECOVERY_SCOPE_005

# In exact generation order. A fifth recovery cannot erase historic latches.
_PREDECESSORS=(*_PRIOR_FOUR,(RECOVERY_SCOPE_004,3))

class V40GuardianRecovery005Halted(RuntimeError):
    """A fixed, nonsecret reason; do not render SQL/network exceptions."""


async def select_fifth_reviewed_scope(
    conn:Any,*,env:Mapping[str,str],
    observe:Callable[[Mapping[str,str]],Awaitable[V40CanaryGuardianHeartbeat]],
    pause:Callable[[float],Awaitable[Any]],
)->tuple[str,str]:
    """Caller MUST already hold guardian's exclusive advisory singleton."""
    def fail(code:str)->None:
        raise V40GuardianRecovery005Halted(code)
    try:
        if (
            conn is None
            or not callable(getattr(conn,"fetchrow",None))
            or not callable(observe) or not callable(pause)
            or env.get("T100_V40_GUARDIAN_AUDITED_ROLLOVER")!="true"
            or env.get("T100_V40_GUARDIAN_RECOVERY_005_OPERATOR_APPROVED")!="true"
        ):
            fail("V40_RECOVERY_005_EXPLICIT_OPERATOR_REVIEW_REQUIRED")
        existing=await conn.fetchrow(
            """SELECT last_status,last_alert,critical_latched
               FROM public.t100_guardian_incident_state
               WHERE scope_sha256=$1""",
            RECOVERY_SCOPE_005,
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
                fail("V40_RECOVERY_005_EXISTING_SCOPE_STATE_INVALID")
            return RECOVERY_SCOPE_005,"EXISTING_RECOVERY_005_SCOPE_STICKY"

        for scope,critical_expected in _PREDECESSORS:
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
                or type(row["revision"]) is not int
                or row["revision"]<3
                or (scope==RECOVERY_SCOPE_004
                    and row["recently_healthy"] is not True)
            ):
                fail("V40_RECOVERY_005_PREDECESSOR_NOT_HEALTHY_OR_STICKY")

            counts=await conn.fetchrow(
                """SELECT
                     COUNT(*) FILTER (
                       WHERE severity='critical'
                     )::int AS total,
                     COUNT(*) FILTER (
                       WHERE severity='critical' AND code=$2
                     )::int AS observer_critical,
                     COUNT(*) FILTER (
                       WHERE severity='critical' AND code<>$2
                     )::int AS other_critical,
                     COUNT(*) FILTER (
                       WHERE delivered_at IS NULL
                     )::int AS pending,
                     COUNT(*) FILTER (
                       WHERE severity='critical'
                         AND created_at >=
                           TIMESTAMPTZ '2026-11-15 12:14:20+00'
                         AND created_at <=
                           TIMESTAMPTZ '2026-11-15 12:14:45+00'
                     )::int AS nov15_incident_in_window,
                     COUNT(*) FILTER (
                       WHERE severity='critical'
                         AND created_at >=
                           TIMESTAMPTZ '2026-11-16 13:15:25+00'
                         AND created_at <=
                           TIMESTAMPTZ '2026-11-16 13:15:45+00'
                     )::int AS nov16_incident_in_window,
                     COUNT(*) FILTER (
                       WHERE severity='critical'
                         AND created_at >=
                           TIMESTAMPTZ '2026-11-17 11:49:30+00'
                         AND created_at <=
                           TIMESTAMPTZ '2026-11-17 11:49:50+00'
                     )::int AS nov17_incident_in_window
                   FROM public.t100_guardian_incident_outbox
                   WHERE scope_sha256=$1""",
                scope,HISTORIC_CRITICAL,
            )
            if (
                counts is None
                or type(counts["total"]) is not int
                or counts["total"]!=critical_expected
                or type(counts["observer_critical"]) is not int
                or counts["observer_critical"]!=critical_expected
                or type(counts["other_critical"]) is not int
                or counts["other_critical"]!=0
                or type(counts["pending"]) is not int
                or counts["pending"]!=0
                or any(type(counts[key]) is not int for key in (
                    "nov15_incident_in_window",
                    "nov16_incident_in_window",
                    "nov17_incident_in_window",
                ))
                or (
                    scope==RECOVERY_SCOPE_004 and any(
                        counts[key]!=1 for key in (
                            "nov15_incident_in_window",
                            "nov16_incident_in_window",
                            "nov17_incident_in_window",
                        )
                    )
                )
                or (
                    scope!=RECOVERY_SCOPE_004 and any(
                        counts[key]!=0 for key in (
                            "nov15_incident_in_window",
                            "nov16_incident_in_window",
                            "nov17_incident_in_window",
                        )
                    )
                )
            ):
                fail("V40_RECOVERY_005_INCIDENT_HISTORY_OR_DELIVERY_MISMATCH")

        # Never substitute an old/steady Render heartbeat. Both passes
        # use fresh signed exchange GET and the immutable runtime ledger.
        for i in range(2):
            try:
                h=await observe(env)
            except Exception:
                fail("V40_RECOVERY_005_FRESH_SIGNED_GET_UNAVAILABLE")
            if (
                type(h) is not V40CanaryGuardianHeartbeat
                or h.status!=
                    "V40_GUARDIAN_FLAT_ACCOUNT_DOUBLE_GET_STANDBY_NONAUTHORIZING"
                or h.monitored_account_flat is not True
                or h.inventory_two_signed_get_bookends is not True
                or h.hedge_mode_verified is not True
                or h.ledger_provenance_verified is not True
                or h.incident_alert is not False
                or h.mexc_posts!=0 or h.database_writes!=0
            ):
                fail("V40_RECOVERY_005_FRESH_FLAT_HEDGE_LEDGER_UNVERIFIED")
            if i==0:
                await pause(3.0)

        outbox=GuardianIncidentOutbox(conn,scope_sha256=RECOVERY_SCOPE_005)
        state=await outbox.observe(status=AUDIT_STATUS,alert=False)
        if state.critical_latched is not False or state.revision!=1:
            fail("V40_RECOVERY_005_NEW_SCOPE_NOT_CLEAN")
        return (
            RECOVERY_SCOPE_005,
            "NEW_RECOVERY_005_AFTER_FIVE_REVIEWED_SCOPES_AND_DOUBLE_SIGNED_GET",
        )
    except V40GuardianRecovery005Halted:
        raise
    except Exception:
        raise V40GuardianRecovery005Halted(
            "V40_RECOVERY_005_DB_OR_ACCOUNT_REVIEW_UNAVAILABLE"
        ) from None
