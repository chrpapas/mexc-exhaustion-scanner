"""Independent current guardian owned-position health for V40 close-only watch.

No POST and no attempt to revive a historical sticky incident. This guard
accepts ONLY a fresh, unlatched, independently verified exactly-one-owned
CROSS short with full-volume native market stop and zero pending alerts.
Any missing/stale or latched observation HALTS active supervision, even if
the exchange appears flat in an unrelated GET.
"""
from __future__ import annotations
from dataclasses import dataclass
from typing import Any
from app.t100_live_db_inspect import EXPECTED_DATABASE
from app.t100_live_role_preflight import TARGET_ROLE
from app.t100_v40_guardian_reviewed_generation import ACTIVE_REVIEWED_SCOPE


@dataclass(frozen=True)
class V40OwnedGuardianHealth:
    status:str
    owned_cross_native_sl_observed:bool=False
    fresh:bool=False
    incident_clear:bool=False
    mexc_posts:int=0
    database_writes:int=0
    entry_allowed:bool=False
    live_authorization:bool=False


class PostgresV40OwnedGuardianGate:
    def __init__(self,pool:Any):
        if pool is None:
            raise ValueError("V40_OWNED_GUARDIAN_POOL_REQUIRED")
        self.pool=pool

    async def verify(self)->V40OwnedGuardianHealth:
        block=lambda status:V40OwnedGuardianHealth(status)
        try:
            async with self.pool.acquire() as conn:
                async with conn.transaction(
                    isolation="repeatable_read",readonly=True,
                ):
                    if (
                        await conn.fetchval("SELECT current_user")!=TARGET_ROLE
                        or await conn.fetchval("SELECT current_database()")
                            !=EXPECTED_DATABASE
                        or await conn.fetchval(
                            "SELECT current_setting('transaction_read_only')"
                        )!="on"
                        or await conn.fetchval(
                            "SELECT current_setting('transaction_isolation')"
                        )!="repeatable read"
                    ):
                        return block("V40_OWNED_GUARDIAN_RESTRICTED_SQL_UNVERIFIED")
                    state=await conn.fetchrow(
                        """SELECT last_status,last_alert,critical_latched,
                           revision,
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
                        return block("V40_OWNED_GUARDIAN_NOT_CURRENT_OR_LATCHED")
                    pending=await conn.fetchrow(
                        """SELECT
                           COUNT(*) FILTER (WHERE severity='critical')::int
                             AS critical_total,
                           COUNT(*) FILTER (WHERE delivered_at IS NULL)::int
                             AS pending_total
                           FROM public.t100_guardian_incident_outbox
                           WHERE scope_sha256=$1""",
                        ACTIVE_REVIEWED_SCOPE,
                    )
                    if (
                        pending is None
                        or pending["critical_total"]!=0
                        or pending["pending_total"]!=0
                    ):
                        return block("V40_OWNED_GUARDIAN_ALERTS_UNRESOLVED")
        except Exception:
            return block("V40_OWNED_GUARDIAN_DB_UNAVAILABLE")
        return V40OwnedGuardianHealth(
            "V40_ONE_OWNED_CROSS_NATIVE_MARKET_SL_INDEPENDENTLY_OBSERVED",
            owned_cross_native_sl_observed=True,
            fresh=True,incident_clear=True,
        )
