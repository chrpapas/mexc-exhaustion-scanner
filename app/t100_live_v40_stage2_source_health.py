"""Separate SELECT-only scanner heartbeat and Stage2 frequency diagnostic.

This is for manual operator research after a valid empty *fresh* Stage2
batch; NEVER feeds candidate intake, trading decisions, or any live permits.
A heartbeat is an observation of an update, not proof that scanner/data
ingestion is currently healthy or that old signals are tradable.

Only the existing scanner-owner login is used, with an independently
checked REPEATABLE READ / READ ONLY transaction, correct DB and role. No
SQL mutation, MEXC request, Keychain lookup or credentials in responses.
"""
from __future__ import annotations

from dataclasses import dataclass
from collections.abc import Mapping
from datetime import datetime,timedelta,timezone
from typing import Any

from app.t100_live_db_inspect import EXPECTED_DATABASE
from app.t100_live_v40_stage2_source import (
    MANUAL_SCANNER_READONLY_OWNER_ROLE,PostgresV40Stage2Source,
)

HEARTBEAT_SQL = """
SELECT last_seen_at
FROM public.worker_heartbeat
WHERE worker_name='mexc-t100-200-scanner'
"""
RUNTIME_SQL = """
SELECT last_eval_at
FROM public.t100_runtime
WHERE singleton=true
"""
RECENT_ELIGIBLE_SQL = """
SELECT count(*)::bigint
FROM public.t100_p2_signals
WHERE eligible_stage2=true
  AND p2_at >= $1 AND p2_at <= $2
"""
LAST_ELIGIBLE_SQL = """
SELECT p2_at
FROM public.t100_p2_signals
WHERE eligible_stage2=true
ORDER BY p2_at DESC,id DESC
LIMIT 1
"""
LOOKBACK_DAYS=7
# A scanner may be completing a 30m cycle. This is deliberately
# observational and does not certify uptime. A >60m eval age deserves
# investigation even if the heartbeat continues refreshing.
HEARTBEAT_RECENT_SECONDS=600
EVAL_RECENT_SECONDS=3600


@dataclass(frozen=True)
class V40Stage2SourceHealth:
    status: str
    database_and_scanner_role_verified: bool=False
    scanner_heartbeat_utc: str|None=None
    scanner_last_eval_utc: str|None=None
    latest_eligible_stage2_utc: str|None=None
    eligible_stage2_events_last_7d: int|None=None
    scanner_heartbeat_recent: bool=False
    scanner_evaluation_recent: bool=False
    scanner_process_healthy_certified: bool=False
    ingestion_completeness_certified: bool=False
    live_signal_tradable: bool=False
    live_authorization: bool=False
    entry_allowed: bool=False
    database_writes: int=0
    mexc_posts: int=0
    read_only: bool=True


def _utc(value:Any,now:datetime)->tuple[str|None,bool]:
    if value is None:
        return None,False
    if type(value) is not datetime or value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("BAD_SOURCE_TIMESTAMP")
    ts=value.astimezone(timezone.utc)
    if ts>now+timedelta(seconds=30):
        raise ValueError("FUTURE_SOURCE_TIMESTAMP")
    return ts.isoformat(),True


async def inspect_scanner_source_health(
    source:PostgresV40Stage2Source,*,observed_at:datetime,
)->V40Stage2SourceHealth:
    """Perform 4 fixed SELECTs only; never convert absence to a trading pass."""
    def blocked(code:str)->V40Stage2SourceHealth:
        return V40Stage2SourceHealth(status=code)

    if (
        type(source) is not PostgresV40Stage2Source
        or source.manual_scanner_owner_readonly is not True
        or source.expected_source_role!=MANUAL_SCANNER_READONLY_OWNER_ROLE
        or type(observed_at) is not datetime
        or observed_at.tzinfo is None or observed_at.utcoffset() is None
    ):
        return blocked("STAGE2_SOURCE_HEALTH_OWNER_READONLY_REQUIRED_HALT")
    now=observed_at.astimezone(timezone.utc)
    if now.year<2020:
        return blocked("STAGE2_SOURCE_HEALTH_CLOCK_INVALID_HALT")
    try:
        async with source.pool.acquire() as conn:
            async with conn.transaction(isolation="repeatable_read",readonly=True):
                if (
                    await conn.fetchval("SELECT current_setting('transaction_read_only')")!="on"
                    or await conn.fetchval("SELECT current_setting('transaction_isolation')")!="repeatable read"
                    or await conn.fetchval("SELECT current_database()")!=EXPECTED_DATABASE
                    or await conn.fetchval("SELECT current_user")!=MANUAL_SCANNER_READONLY_OWNER_ROLE
                ):
                    return blocked("STAGE2_SOURCE_HEALTH_READONLY_IDENTITY_UNVERIFIED_HALT")

                heartbeat=await conn.fetchrow(HEARTBEAT_SQL)
                runtime=await conn.fetchrow(RUNTIME_SQL)
                count=await conn.fetchval(
                    RECENT_ELIGIBLE_SQL,now-timedelta(days=LOOKBACK_DAYS),now,
                )
                latest=await conn.fetchrow(LAST_ELIGIBLE_SQL)

        if (
            (heartbeat is not None and not isinstance(heartbeat,Mapping))
            or (runtime is not None and not isinstance(runtime,Mapping))
            or (latest is not None and not isinstance(latest,Mapping))
            or type(count) is not int or count<0
        ):
            return blocked("STAGE2_SOURCE_HEALTH_READBACK_INVALID_HALT")

        hb=heartbeat.get("last_seen_at") if heartbeat is not None else None
        ev=runtime.get("last_eval_at") if runtime is not None else None
        last=latest.get("p2_at") if latest is not None else None
        hb_utc,_=_utc(hb,now)
        ev_utc,_=_utc(ev,now)
        last_utc,_=_utc(last,now)
        hb_recent=bool(
            hb is not None
            and (now-hb.astimezone(timezone.utc)).total_seconds()
            <=HEARTBEAT_RECENT_SECONDS
        )
        eval_recent=bool(
            ev is not None
            and (now-ev.astimezone(timezone.utc)).total_seconds()
            <=EVAL_RECENT_SECONDS
        )
        return V40Stage2SourceHealth(
            status=(
                "STAGE2_SCANNER_RECENT_HEARTBEAT_AND_EVALUATION_OBSERVED_NONAUTHORIZING"
                if hb_recent and eval_recent else
                "STAGE2_SCANNER_HEARTBEAT_OR_EVALUATION_NOT_RECENT_NONAUTHORIZING"
            ),
            database_and_scanner_role_verified=True,
            scanner_heartbeat_utc=hb_utc,
            scanner_last_eval_utc=ev_utc,
            latest_eligible_stage2_utc=last_utc,
            eligible_stage2_events_last_7d=count,
            scanner_heartbeat_recent=hb_recent,
            scanner_evaluation_recent=eval_recent,
        )
    except Exception:
        # Never expose driver SQL or DSNs/passwords.
        return blocked("STAGE2_SOURCE_HEALTH_READONLY_INSPECTION_FAILED_HALT")
