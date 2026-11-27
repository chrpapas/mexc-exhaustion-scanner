"""Audited V40 guardian 008: preserve Nov23 stops HTTP-5xx critical.

Existing scope 007 remains permanently sticky after the *delivered* critical
FIRST_STOPS HTTP-5xx at 2026-11-23T03:00:28Z. All seven historical scope
records and alerts remain unchanged. Under the exclusive singleton,
verify exact incident counts, classification, time windows, delivery, and
two fresh signed FULL-account plus immutable-ledger FLAT bookends before
creating new 008. No deletion, unsafe retry, order post or guard bypass.
"""
from __future__ import annotations

from hashlib import sha256
from typing import Any, Awaitable, Callable, Mapping

from app.t100_v40_canary_account_guardian import V40CanaryGuardianHeartbeat
from app.t100_live_guardian_incident_outbox import GuardianIncidentOutbox
from app.t100_v40_guardian_recovery_005 import (
    RECOVERY_SCOPE_005,_PREDECESSORS as _PRIOR_FIVE,
)
from app.t100_v40_guardian_recovery_004 import RECOVERY_SCOPE_004
from app.t100_v40_guardian_recovery_006 import RECOVERY_SCOPE_006
from app.t100_v40_guardian_recovery_007 import RECOVERY_SCOPE_007
from app.t100_v40_guardian_reviewed_generation import (
    AUDIT_STATUS,GOOD_STATUS,HISTORIC_CRITICAL,
)

RECOVERY_SCOPE_008=sha256(
    b"T100_V40_DEDICATED_GUARDIAN_REVIEWED_STOPS_HTTP_5XX_2026_11_23_008"
).hexdigest()
ACTIVE_REVIEWED_SCOPE=RECOVERY_SCOPE_008
_OLD_SCOPES=(*_PRIOR_FIVE,(RECOVERY_SCOPE_005,2),
             (RECOVERY_SCOPE_006,1),(RECOVERY_SCOPE_007,1))

class V40GuardianRecovery008Halted(RuntimeError):
    """Fixed safe code; never include raw SQL, MEXC or account data."""


async def select_eighth_reviewed_scope(
    conn:Any,*,env:Mapping[str,str],
    observe:Callable[[Mapping[str,str]],Awaitable[V40CanaryGuardianHeartbeat]],
    pause:Callable[[float],Awaitable[Any]],
)->tuple[str,str]:
    """Must run inside the guardian's EXCLUSIVE PostgreSQL singleton."""
    def fail(message:str)->None:
        raise V40GuardianRecovery008Halted(message)
    try:
        if (
            conn is None or not callable(getattr(conn,"fetchrow",None))
            or not callable(observe) or not callable(pause)
            or env.get("T100_V40_GUARDIAN_AUDITED_ROLLOVER")!="true"
            or env.get("T100_V40_GUARDIAN_RECOVERY_005_OPERATOR_APPROVED")!="true"
            or env.get("T100_V40_GUARDIAN_RECOVERY_006_OPERATOR_APPROVED")!="true"
            or env.get("T100_V40_GUARDIAN_RECOVERY_007_OPERATOR_APPROVED")!="true"
            or env.get("T100_V40_GUARDIAN_RECOVERY_008_OPERATOR_APPROVED")!="true"
        ):
            fail("V40_RECOVERY_008_EXPLICIT_OPERATOR_APPROVAL_MISSING")
        existing=await conn.fetchrow(
            """SELECT last_status,last_alert,critical_latched
               FROM public.t100_guardian_incident_state
               WHERE scope_sha256=$1""",RECOVERY_SCOPE_008,
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
                fail("V40_RECOVERY_008_EXISTING_SCOPE_UNKNOWN")
            return RECOVERY_SCOPE_008,"EXISTING_RECOVERY_008_SCOPE_STICKY"

        for scope,expected in _OLD_SCOPES:
            state=await conn.fetchrow(
                """SELECT last_status,last_alert,critical_latched,revision,
                          observed_at>=clock_timestamp()
                            -interval '120 seconds' AS recently_healthy
                   FROM public.t100_guardian_incident_state
                   WHERE scope_sha256=$1""",scope,
            )
            if (
                state is None
                or state["last_status"]!=GOOD_STATUS
                or state["last_alert"] is not False
                or state["critical_latched"] is not True
                or type(state["revision"]) is not int or state["revision"]<3
            ):
                fail("V40_RECOVERY_008_OLD_STATE_OR_STICKY_LATCH_CHANGED")
            # A new Render deployment retires the 006 observer BEFORE 007
            # can acquire the exclusive PG singleton. Requiring the retired
            # observer's LAST heartbeat <120s is therefore a deadlock under
            # healthy deployment or a prolonged singleton handoff. The
            # 006 state MUST still be last healthy FLAT, its old critical
            # MUST stay sticky and exact, and the new 007 scope is initialized
            # only AFTER TWO independent live signed-GET/immutable-ledger
            # bookends below. No stale sample substitutes those fresh GETs.

            counts=await conn.fetchrow(
                """SELECT
                     COUNT(*) FILTER (WHERE severity='critical')::int AS total,
                     COUNT(*) FILTER (WHERE severity='critical' AND code=$2)
                        ::int AS observer_critical,
                     COUNT(*) FILTER (WHERE severity='critical' AND code<>$2)
                        ::int AS other_critical,
                     COUNT(*) FILTER (WHERE delivered_at IS NULL)
                        ::int AS pending,
                     COUNT(*) FILTER (
                        WHERE severity='critical'
                        AND created_at>=TIMESTAMPTZ '2026-11-19 08:15:25+00'
                        AND created_at<=TIMESTAMPTZ '2026-11-19 08:15:32+00'
                     )::int AS nov19_open_orders_http_5xx,
                     COUNT(*) FILTER (
                        WHERE severity='critical'
                        AND created_at>=TIMESTAMPTZ '2026-11-19 08:16:05+00'
                        AND created_at<=TIMESTAMPTZ '2026-11-19 08:16:15+00'
                     )::int AS nov19_after_positions_http_5xx,
                     COUNT(*) FILTER (
                        WHERE severity='critical'
                        AND created_at>=TIMESTAMPTZ '2026-11-23 03:00:24+00'
                        AND created_at<=TIMESTAMPTZ '2026-11-23 03:00:33+00'
                     )::int AS nov23_first_stops_http_5xx,
                     COUNT(*) FILTER (
                        WHERE severity='critical'
                        AND created_at>=TIMESTAMPTZ '2026-11-21 22:03:18+00'
                        AND created_at<=TIMESTAMPTZ '2026-11-21 22:03:32+00'
                     )::int AS nov21_before_positions_timeout,
                     COUNT(*) FILTER (
                        WHERE severity='critical'
                        AND created_at>=TIMESTAMPTZ '2026-11-15 12:14:20+00'
                        AND created_at<=TIMESTAMPTZ '2026-11-15 12:14:45+00'
                     )::int AS nov15_window,
                     COUNT(*) FILTER (
                        WHERE severity='critical'
                        AND created_at>=TIMESTAMPTZ '2026-11-16 13:15:25+00'
                        AND created_at<=TIMESTAMPTZ '2026-11-16 13:15:45+00'
                     )::int AS nov16_window,
                     COUNT(*) FILTER (
                        WHERE severity='critical'
                        AND created_at>=TIMESTAMPTZ '2026-11-17 11:49:30+00'
                        AND created_at<=TIMESTAMPTZ '2026-11-17 11:49:50+00'
                     )::int AS nov17_window
                   FROM public.t100_guardian_incident_outbox
                   WHERE scope_sha256=$1""",scope,HISTORIC_CRITICAL,
            )
            # Emit a finite *category*, never IDs, SQL, timestamps or
            # counts. Distinguish real 005 incident-history mismatch from
            # a stale legacy epoch without weakening any predicate.
            if counts is None or any(type(counts[k]) is not int for k in (
                "total","observer_critical","other_critical","pending",
                "nov19_open_orders_http_5xx","nov19_after_positions_http_5xx",
                "nov21_before_positions_timeout",
                "nov23_first_stops_http_5xx",
                "nov15_window",
                "nov16_window","nov17_window",
            )):
                fail("V40_RECOVERY_008_OUTBOX_SHAPE_INVALID")
            if (
                counts["total"]!=expected
                or counts["observer_critical"]!=expected
                or counts["other_critical"]!=0
            ):
                fail(
                    "V40_RECOVERY_008_005_CRITICAL_CLASS_OR_COUNT_MISMATCH"
                    if scope==RECOVERY_SCOPE_005 else
                    "V40_RECOVERY_008_OLDER_CRITICAL_CLASS_OR_COUNT_MISMATCH"
                )
            if counts["pending"]!=0:
                fail("V40_RECOVERY_008_DISCORD_PENDING_INCIDENT")
            if counts["nov21_before_positions_timeout"]!=(
                1 if scope==RECOVERY_SCOPE_006 else 0
            ):
                fail("V40_RECOVERY_008_006_TIMEOUT_INCIDENT_MISMATCH")
            if counts["nov23_first_stops_http_5xx"]!=(
                1 if scope==RECOVERY_SCOPE_007 else 0
            ):
                fail("V40_RECOVERY_008_007_STOPS_5XX_INCIDENT_MISMATCH")
            if any(
                counts[k]!=(1 if scope==RECOVERY_SCOPE_005 else 0)
                for k in (
                    "nov19_open_orders_http_5xx",
                    "nov19_after_positions_http_5xx",
                )
            ):
                fail(
                    "V40_RECOVERY_008_005_HTTP_5XX_TIME_WINDOW_MISMATCH"
                    if scope==RECOVERY_SCOPE_005 else
                    "V40_RECOVERY_008_OTHER_EPOCH_NOV19_CRITICAL_PRESENT"
                )
            if any(
                counts[k]!=(1 if scope==RECOVERY_SCOPE_004 else 0)
                for k in ("nov15_window","nov16_window","nov17_window")
            ):
                fail("V40_RECOVERY_008_OLD_INCIDENT_TIME_WINDOWS_MISMATCH")

        # New 007 is allowed only after independent signed full exchange
        # and durable ledger bookends twice, not after an old heartbeat.
        for n in range(2):
            try:
                beat=await observe(env)
            except Exception:
                fail("V40_RECOVERY_008_SIGNED_GET_FAILED")
            if (
                type(beat) is not V40CanaryGuardianHeartbeat
                or beat.status!=
                   "V40_GUARDIAN_FLAT_ACCOUNT_DOUBLE_GET_STANDBY_NONAUTHORIZING"
                or beat.monitored_account_flat is not True
                or beat.inventory_two_signed_get_bookends is not True
                or beat.hedge_mode_verified is not True
                or beat.ledger_provenance_verified is not True
                or beat.incident_alert is not False
                or beat.mexc_posts!=0 or beat.database_writes!=0
            ):
                fail("V40_RECOVERY_008_CURRENT_FLAT_LEDGER_NOT_VERIFIED")
            if n==0:
                await pause(3.0)
        outbox=GuardianIncidentOutbox(conn,scope_sha256=RECOVERY_SCOPE_008)
        state=await outbox.observe(status=AUDIT_STATUS,alert=False)
        if state.critical_latched is not False or state.revision!=1:
            fail("V40_RECOVERY_008_NEW_SCOPE_NOT_CLEAN")
        return (
            RECOVERY_SCOPE_008,
            "NEW_RECOVERY_008_AFTER_EXACT_007_STOPS_5XX_AND_FRESH_FLAT",
        )
    except V40GuardianRecovery008Halted:
        raise
    except Exception:
        raise V40GuardianRecovery008Halted(
            "V40_RECOVERY_008_DB_OR_LIVE_ACCOUNT_REVIEW_UNAVAILABLE"
        ) from None
