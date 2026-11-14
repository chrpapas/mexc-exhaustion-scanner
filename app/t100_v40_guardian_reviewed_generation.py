"""V40 incident-preserving, explicitly opted-in first-canary recovery epoch.

This is not an UPDATE of a latched critical incident. It leaves the original
scope, critical state, and all Discord outbox evidence untouched. A separate
reviewed scope may be initialized exactly once after proving:
- the only historic critical incident class was GET/inventory unverified
  (never a known unexpected position or missing native SL);
- every historic notification was acknowledged;
- the original guardian recently reported a verified flat, hedge account;
- two fresh independent signed GET / immutable-ledger inspections report FLAT.
The singleton lock is owned by the caller throughout the transition.

No MEXC POST, no trade authorization, no SQL schema changes, and no deletion
or clearing of sticky critical states is performed here.
"""
from __future__ import annotations

import hashlib
from typing import Any,Awaitable,Callable,Mapping

from app.t100_v40_canary_account_guardian import V40CanaryGuardianHeartbeat
from app.t100_live_guardian_incident_outbox import GuardianIncidentOutbox

LEGACY_SCOPE=hashlib.sha256(
    b"T100_V40_DEDICATED_FIRST_CANARY_GUARDIAN"
).hexdigest()
REVIEWED_SCOPE=hashlib.sha256(
    b"T100_V40_DEDICATED_FIRST_CANARY_GUARDIAN_REVIEWED_2026_11_13_001"
).hexdigest()
AUDIT_STATUS="OBSERVER_V40_CANARY_AUDITED_ROLLOVER"
GOOD_STATUS="OBSERVER_V40_CANARY_FLAT_DOUBLE_GET"
HISTORIC_CRITICAL="OBSERVER_V40_CANARY_SIGNED_GET_OR_ACCOUNT_INVALID"


class V40GuardianReviewedGenerationHalted(RuntimeError):
    pass


async def select_reviewed_scope(
    conn:Any,
    *,
    env:Mapping[str,str],
    observe:Callable[[Mapping[str,str]],Awaitable[V40CanaryGuardianHeartbeat]],
    pause:Callable[[float],Awaitable[Any]],
)->tuple[str,str]:
    """SELECT-only preflight; a new scope is initialized only after two GETs.

    Return (scope, outcome). Called with the exclusive incident advisory lock
    already acquired; do not invoke independently of the guardian runtime.
    """
    try:
        existing=await conn.fetchrow(
            """SELECT last_status,last_alert,critical_latched
               FROM public.t100_guardian_incident_state
               WHERE scope_sha256=$1""",
            REVIEWED_SCOPE,
        )
        if existing is not None:
            # Never re-initialize/reset a second-generation critical latch.
            # An already-created generation must keep its old state.
            if existing["last_status"] not in (AUDIT_STATUS,
                    GOOD_STATUS,
                    "OBSERVER_V40_CANARY_SIGNED_GET_OR_ACCOUNT_INVALID",
                    "OBSERVER_V40_CANARY_NONFLAT_UNKNOWN_OWNER",
                    "OBSERVER_V40_CANARY_OWNED_CROSS_NATIVE_MARKET_SL"):
                raise V40GuardianReviewedGenerationHalted(
                    "V40_AUDITED_ROLLOVER_REVIEWED_STATE_INVALID"
                )
            return REVIEWED_SCOPE,"EXISTING_REVIEWED_GENERATION"

        prior=await conn.fetchrow(
            """SELECT last_status,last_alert,critical_latched,revision,
                      observed_at >= clock_timestamp() - interval '120 seconds'
                          AS recently_healthy
               FROM public.t100_guardian_incident_state
               WHERE scope_sha256=$1""",
            LEGACY_SCOPE,
        )
        if (
            prior is None
            or prior["last_status"]!=GOOD_STATUS
            or prior["last_alert"] is not False
            or prior["critical_latched"] is not True
            or type(prior["revision"]) is not int
            or prior["revision"]<3
            or prior["recently_healthy"] is not True
        ):
            raise V40GuardianReviewedGenerationHalted(
                "V40_AUDITED_ROLLOVER_LEGACY_NOT_RECOVERED"
            )

        incidents=await conn.fetchrow(
            """SELECT
                 COUNT(*) FILTER (WHERE severity='critical')::int
                   AS critical_total,
                 COUNT(*) FILTER (WHERE severity='critical'
                                      AND code<>$2)::int
                   AS forbidden_critical,
                 COUNT(*) FILTER (WHERE delivered_at IS NULL)::int
                   AS undelivered
               FROM public.t100_guardian_incident_outbox
               WHERE scope_sha256=$1""",
            LEGACY_SCOPE,HISTORIC_CRITICAL,
        )
        if (
            incidents is None
            or type(incidents["critical_total"]) is not int
            or incidents["critical_total"]!=1
            or incidents["forbidden_critical"]!=0
            or incidents["undelivered"]!=0
        ):
            raise V40GuardianReviewedGenerationHalted(
                "V40_AUDITED_ROLLOVER_UNREVIEWED_ALERTS"
            )

        # This is NOT solely trusting stale DB heartbeats. Verify current
        # account/ledger and native pending orders twice with a small gap.
        for cycle in (1,2):
            try:
                heartbeat=await observe(env)
            except Exception:
                raise V40GuardianReviewedGenerationHalted(
                    "V40_AUDITED_ROLLOVER_FRESH_FLAT_UNVERIFIED"
                ) from None
            if (
                type(heartbeat) is not V40CanaryGuardianHeartbeat
                or heartbeat.status!=
                    "V40_GUARDIAN_FLAT_ACCOUNT_DOUBLE_GET_STANDBY_NONAUTHORIZING"
                or heartbeat.monitored_account_flat is not True
                or heartbeat.inventory_two_signed_get_bookends is not True
                or heartbeat.hedge_mode_verified is not True
                or heartbeat.incident_alert is not False
                or heartbeat.mexc_posts!=0
                or heartbeat.database_writes!=0
            ):
                raise V40GuardianReviewedGenerationHalted(
                    "V40_AUDITED_ROLLOVER_FRESH_FLAT_UNVERIFIED"
                )
            if cycle==1:
                await pause(3.0)
        # Audited initial event is a new immutable scope; old critical
        # and outbox rows are never UPDATEd, DELETEd or truncated.
        reviewed=GuardianIncidentOutbox(conn,scope_sha256=REVIEWED_SCOPE)
        state=await reviewed.observe(status=AUDIT_STATUS,alert=False)
        if state.critical_latched is not False or state.revision!=1:
            raise V40GuardianReviewedGenerationHalted(
                "V40_AUDITED_ROLLOVER_NEW_SCOPE_NOT_CLEAN"
            )
        return REVIEWED_SCOPE,"NEW_REVIEWED_GENERATION_AFTER_DOUBLE_GET"
    except V40GuardianReviewedGenerationHalted:
        raise
    except Exception:
        # Database/HTTP exceptions must never leak user URLs/credentials.
        raise V40GuardianReviewedGenerationHalted(
            "V40_AUDITED_ROLLOVER_DATABASE_REVIEW_UNAVAILABLE"
        ) from None

# One additional, explicitly fixed epoch for the single 2026-11-13 transient
# GET/ledger incident in REVIEWED_SCOPE. Both older scopes remain immutable.
# A failure in this epoch is permanently sticky: never synthesize a third.
RECOVERY_SCOPE_002=hashlib.sha256(
    b"T100_V40_DEDICATED_FIRST_CANARY_GUARDIAN_REVIEWED_2026_11_13_002"
).hexdigest()
ACTIVE_REVIEWED_SCOPE=RECOVERY_SCOPE_002


async def select_second_reviewed_scope(
    conn:Any,
    *,
    env:Mapping[str,str],
    observe:Callable[[Mapping[str,str]],Awaitable[V40CanaryGuardianHeartbeat]],
    pause:Callable[[float],Awaitable[Any]],
)->tuple[str,str]:
    """Recover one verified transient failure without clearing incident history.

    Safe ONLY when both prior monitoring epochs have exactly one delivered
    GET/inventory-unverified critical incident apiece, their last observations
    are flat, and the reviewed 001 epoch remains recently healthy. A new 002
    scope is inserted only after two additional live signed GET/ledger passes.
    Restarting an existing 002 scope never clears its critical latch.
    """
    try:
        existing=await conn.fetchrow(
            """SELECT last_status,last_alert,critical_latched
               FROM public.t100_guardian_incident_state
               WHERE scope_sha256=$1""",
            RECOVERY_SCOPE_002,
        )
        if existing is not None:
            if existing["last_status"] not in (
                AUDIT_STATUS,GOOD_STATUS,HISTORIC_CRITICAL,
                "OBSERVER_V40_CANARY_NONFLAT_UNKNOWN_OWNER",
                "OBSERVER_V40_CANARY_OWNED_CROSS_NATIVE_MARKET_SL",
            ):
                raise V40GuardianReviewedGenerationHalted(
                    "V40_AUDITED_ROLLOVER_REVIEWED_STATE_INVALID"
                )
            return RECOVERY_SCOPE_002,"EXISTING_RECOVERY_002_SCOPE"

        # Require both predecessor epochs and the full preserved alert trail.
        # The legacy epoch is archived; its timestamp need not be fresh.
        legacy=await conn.fetchrow(
            """SELECT last_status,last_alert,critical_latched
               FROM public.t100_guardian_incident_state
               WHERE scope_sha256=$1""",
            LEGACY_SCOPE,
        )
        prior=await conn.fetchrow(
            """SELECT last_status,last_alert,critical_latched,revision,
                      observed_at >= clock_timestamp() - interval '120 seconds'
                          AS recently_healthy
               FROM public.t100_guardian_incident_state
               WHERE scope_sha256=$1""",
            REVIEWED_SCOPE,
        )
        if (
            legacy is None
            or legacy["last_status"]!=GOOD_STATUS
            or legacy["last_alert"] is not False
            or legacy["critical_latched"] is not True
            or prior is None
            or prior["last_status"]!=GOOD_STATUS
            or prior["last_alert"] is not False
            or prior["critical_latched"] is not True
            or type(prior["revision"]) is not int
            or prior["revision"]<3
            or prior["recently_healthy"] is not True
        ):
            raise V40GuardianReviewedGenerationHalted(
                "V40_AUDITED_ROLLOVER_LEGACY_NOT_RECOVERED"
            )
        for scope in (LEGACY_SCOPE,REVIEWED_SCOPE):
            incidents=await conn.fetchrow(
                """SELECT
                     COUNT(*) FILTER (WHERE severity='critical')::int
                       AS critical_total,
                     COUNT(*) FILTER (WHERE severity='critical'
                                         AND code<>$2)::int
                       AS forbidden_critical,
                     COUNT(*) FILTER (WHERE delivered_at IS NULL)::int
                       AS undelivered
                   FROM public.t100_guardian_incident_outbox
                   WHERE scope_sha256=$1""",
                scope,HISTORIC_CRITICAL,
            )
            if (
                incidents is None
                or type(incidents["critical_total"]) is not int
                or incidents["critical_total"]!=1
                or incidents["forbidden_critical"]!=0
                or incidents["undelivered"]!=0
            ):
                raise V40GuardianReviewedGenerationHalted(
                    "V40_AUDITED_ROLLOVER_UNREVIEWED_ALERTS"
                )

        # Fresh verified account/ledger reads are mandatory. A stale
        # Render heartbeat or environment variable never counts as proof.
        for cycle in (1,2):
            try:
                heartbeat=await observe(env)
            except Exception:
                raise V40GuardianReviewedGenerationHalted(
                    "V40_AUDITED_ROLLOVER_FRESH_FLAT_UNVERIFIED"
                ) from None
            if (
                type(heartbeat) is not V40CanaryGuardianHeartbeat
                or heartbeat.status!=
                    "V40_GUARDIAN_FLAT_ACCOUNT_DOUBLE_GET_STANDBY_NONAUTHORIZING"
                or heartbeat.monitored_account_flat is not True
                or heartbeat.inventory_two_signed_get_bookends is not True
                or heartbeat.hedge_mode_verified is not True
                or heartbeat.incident_alert is not False
                or heartbeat.mexc_posts!=0
                or heartbeat.database_writes!=0
            ):
                raise V40GuardianReviewedGenerationHalted(
                    "V40_AUDITED_ROLLOVER_FRESH_FLAT_UNVERIFIED"
                )
            if cycle==1:
                await pause(3.0)
        outbox=GuardianIncidentOutbox(conn,scope_sha256=RECOVERY_SCOPE_002)
        state=await outbox.observe(status=AUDIT_STATUS,alert=False)
        if state.critical_latched is not False or state.revision!=1:
            raise V40GuardianReviewedGenerationHalted(
                "V40_AUDITED_ROLLOVER_NEW_SCOPE_NOT_CLEAN"
            )
        return RECOVERY_SCOPE_002,"NEW_RECOVERY_002_AFTER_DOUBLE_GET"
    except V40GuardianReviewedGenerationHalted:
        raise
    except Exception:
        raise V40GuardianReviewedGenerationHalted(
            "V40_AUDITED_ROLLOVER_DATABASE_REVIEW_UNAVAILABLE"
        ) from None
