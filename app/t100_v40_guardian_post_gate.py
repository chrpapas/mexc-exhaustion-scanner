"""PostgreSQL-backed last-moment guardian release gate for ONE V40 canary POST.

This does not authorize a trade. It makes a *fresh*, SELECT-only, audited
check against the independent V40 guardian's immutable incident state and
outbox, using the existing least-privileged runtime DB role. The archived
first-generation critical incident is never cleared. Any new critical
incident in the reviewed generation freezes trading permanently until a
separately approved recovery process.

Never accept caller booleans, environment variables, cached heartbeats or
service uptime as substitute evidence. Failed network/DB checks fail shut.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from app.t100_live_db_inspect import EXPECTED_DATABASE
from app.t100_live_role_preflight import TARGET_ROLE
from app.t100_v40_guardian_reviewed_generation import REVIEWED_SCOPE

# The guardian scans at 3-5 seconds. Allow no stale (>15 sec) readiness.
_MAX_AGE_SECONDS=15


@dataclass(frozen=True)
class V40GuardianPostGateEvidence:
    status: str
    state_verified: bool=False
    outbox_verified: bool=False
    guardian_fresh: bool=False
    critical_latch_absent: bool=False
    entry_allowed: bool=False
    live_authorization: bool=False
    mexc_posts: int=0
    database_writes: int=0


class PostgresV40GuardianPostGate:
    """Check live guardian state using the restricted runtime DB connection."""

    def __init__(self,pool:Any)->None:
        if pool is None or not callable(getattr(pool,"acquire",None)):
            raise ValueError("V40_GUARDIAN_POST_GATE_POOL_REQUIRED")
        self.pool=pool

    async def verify(self)->V40GuardianPostGateEvidence:
        halt=lambda code:V40GuardianPostGateEvidence(code)
        try:
            async with self.pool.acquire() as conn:
                async with conn.transaction(
                    isolation="repeatable_read",readonly=True,
                ):
                    if (
                        await conn.fetchval("SELECT current_user")!=TARGET_ROLE
                        or await conn.fetchval(
                            "SELECT current_database()"
                        )!=EXPECTED_DATABASE
                        or await conn.fetchval(
                            "SELECT current_setting('transaction_read_only')"
                        )!="on"
                        or await conn.fetchval(
                            "SELECT current_setting('transaction_isolation')"
                        )!="repeatable read"
                    ):
                        return halt("V40_GUARDIAN_POST_GATE_DB_ROLE_INVALID_NO_POST")
                    state=await conn.fetchrow(
                        """SELECT last_status,last_alert,critical_latched,
                           revision,
                           observed_at >= clock_timestamp()
                               - interval '15 seconds' AS fresh,
                           observed_at <= clock_timestamp()
                               + interval '1 second' AS not_future
                           FROM public.t100_guardian_incident_state
                           WHERE scope_sha256=$1""",
                        REVIEWED_SCOPE,
                    )
                    if (
                        state is None
                        or state["last_status"]!=
                            "OBSERVER_V40_CANARY_FLAT_DOUBLE_GET"
                        or state["last_alert"] is not False
                        or state["critical_latched"] is not False
                        or type(state["revision"]) is not int
                        or state["revision"]<2
                        or state["fresh"] is not True
                        or state["not_future"] is not True
                    ):
                        return halt(
                            "V40_GUARDIAN_POST_GATE_LATCH_OR_FRESHNESS_HALT"
                        )
                    outbox=await conn.fetchrow(
                        """SELECT
                             COUNT(*) FILTER (
                               WHERE severity='critical'
                             )::int AS critical_total,
                             COUNT(*) FILTER (
                               WHERE delivered_at IS NULL
                             )::int AS pending_total
                           FROM public.t100_guardian_incident_outbox
                           WHERE scope_sha256=$1""",
                        REVIEWED_SCOPE,
                    )
                    if (
                        outbox is None
                        or outbox["critical_total"]!=0
                        or outbox["pending_total"]!=0
                    ):
                        return halt("V40_GUARDIAN_POST_GATE_OUTBOX_HALT")
            return V40GuardianPostGateEvidence(
                status="V40_GUARDIAN_POST_GATE_CURRENT_FLAT_UNLATCHED_NONAUTHORIZING",
                state_verified=True,
                outbox_verified=True,
                guardian_fresh=True,
                critical_latch_absent=True,
            )
        except Exception:
            # Do not emit SQL, connection errors, credentials or raw data.
            return halt("V40_GUARDIAN_POST_GATE_DATABASE_UNAVAILABLE_NO_POST")
