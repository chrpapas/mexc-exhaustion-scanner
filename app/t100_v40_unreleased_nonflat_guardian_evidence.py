"""Read-only freshness/outbox evidence for guardian of already-owned V40 shorts.

The first-canary PostgresV40GuardianPostGate intentionally requires FLAT.
A fully autonomous 1–4-slot strategy cannot use that as proof that a
*nonflat* account is safe. This separate adapter only observes the guardian
that has independently seen owned CROSS native SL positions. It CANNOT
infer how many positions are open or certify their exact IDs, margin or
stop quantity, and NEVER supplies an exchange mutation permit.

Any caller wanting a next entry must independently re-verify complete
account/stop identity via current signed GETs, durable SQL, leverage,
stressed account collateral, exclusive leader fence and fresh signal.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from app.t100_live_db_inspect import EXPECTED_DATABASE
from app.t100_live_role_preflight import TARGET_ROLE
from app.t100_v40_guardian_recovery_008 import ACTIVE_REVIEWED_SCOPE


@dataclass(frozen=True)
class V40NonflatGuardianEvidence:
    status: str
    guardian_protected_observation_fresh: bool = False
    no_current_scope_critical_or_pending_incidents: bool = False
    exact_positions_independently_verified: bool = False
    cross_margin_and_liquidation_verified: bool = False
    exchange_stop_amendments_verified: bool = False
    next_entry_allowed: bool = False
    live_authorization: bool = False
    exchange_posts: int = 0
    database_writes: int = 0


class PostgresV40NonflatGuardianEvidence:
    """Observes only existing final recovery scope: no new scope or reset."""

    def __init__(self,pool:Any)->None:
        if pool is None or not callable(getattr(pool,"acquire",None)):
            raise ValueError("V40_NONFLAT_GUARDIAN_POOL_REQUIRED")
        self.pool=pool

    async def verify(self)->V40NonflatGuardianEvidence:
        halt=lambda code:V40NonflatGuardianEvidence(code)
        try:
            async with self.pool.acquire() as conn:
                async with conn.transaction(
                    isolation="repeatable_read",readonly=True,
                ):
                    if (
                        await conn.fetchval("SELECT current_user")!=TARGET_ROLE
                        or await conn.fetchval("SELECT current_database()")!=EXPECTED_DATABASE
                        or await conn.fetchval(
                            "SELECT current_setting('transaction_read_only')"
                        )!="on"
                        or await conn.fetchval(
                            "SELECT current_setting('transaction_isolation')"
                        )!="repeatable read"
                    ):
                        return halt("V40_NONFLAT_GUARDIAN_DB_ROLE_INVALID_HALT")
                    state=await conn.fetchrow(
                        """SELECT last_status,last_alert,critical_latched,revision,
                           observed_at >= clock_timestamp()
                             - interval '15 seconds' AS fresh,
                           observed_at <= clock_timestamp()
                             + interval '1 second' AS not_future
                           FROM public.t100_guardian_incident_state
                           WHERE scope_sha256=$1""",
                        ACTIVE_REVIEWED_SCOPE,
                    )
                    if (
                        state is None
                        or state["last_status"]!=
                            "OBSERVER_V40_CANARY_OWNED_CROSS_NATIVE_MARKET_SL"
                        or state["last_alert"] is not False
                        or state["critical_latched"] is not False
                        or type(state["revision"]) is not int
                        or state["revision"]<2
                        or state["fresh"] is not True
                        or state["not_future"] is not True
                    ):
                        return halt("V40_NONFLAT_GUARDIAN_NOT_FRESH_OR_OWNED_HALT")
                    alerts=await conn.fetchrow(
                        """SELECT COUNT(*) FILTER (
                             WHERE severity='critical'
                           )::int AS critical_total,
                           COUNT(*) FILTER (
                             WHERE delivered_at IS NULL
                           )::int AS pending_total
                           FROM public.t100_guardian_incident_outbox
                           WHERE scope_sha256=$1""",
                        ACTIVE_REVIEWED_SCOPE,
                    )
                    if (
                        alerts is None
                        or type(alerts["critical_total"]) is not int
                        or type(alerts["pending_total"]) is not int
                        or alerts["critical_total"]!=0
                        or alerts["pending_total"]!=0
                    ):
                        return halt("V40_NONFLAT_GUARDIAN_ALERT_OR_LATCH_HALT")
            return V40NonflatGuardianEvidence(
                "V40_NONFLAT_GUARDIAN_PROTECTED_OBSERVER_FRESH_NONAUTHORIZING",
                guardian_protected_observation_fresh=True,
                no_current_scope_critical_or_pending_incidents=True,
            )
        except Exception:
            return halt("V40_NONFLAT_GUARDIAN_DATABASE_UNAVAILABLE_HALT")
