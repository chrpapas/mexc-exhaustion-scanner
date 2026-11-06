"""Durable guardian incident outbox for separately scoped PostgreSQL writer.

No MEXC I/O, no trading orders, no checkpoint/entry journal changes.
Requires review-only migration 035 and independently provisioned writer role.
The managed position observer's own DB connection remains SELECT-only.

State transitions and outbox inserts are database-atomic via trigger.
Critical safety latch is sticky; a healthy subsequent observation does NOT
clear it. The caller cannot mark an event delivered until notifier ACK.
"""
from __future__ import annotations

from dataclasses import dataclass
import re
from typing import Any


_SCOPE = re.compile(r"[a-f0-9]{64}\Z")
_STATUS = re.compile(r"OBSERVER_[A-Z0-9_]{1,80}\Z")

_STAGE = """
INSERT INTO public.t100_guardian_incident_state
 (scope_sha256,last_status,last_alert,critical_latched)
VALUES ($1,$2,$3,$3)
ON CONFLICT (scope_sha256) DO UPDATE SET
 revision=public.t100_guardian_incident_state.revision+1,
 last_status=EXCLUDED.last_status,
 last_alert=EXCLUDED.last_alert,
 critical_latched=public.t100_guardian_incident_state.critical_latched
                  OR EXCLUDED.last_alert,
 observed_at=clock_timestamp()
RETURNING revision,critical_latched
"""
_PENDING = """
SELECT seq,code,severity,attempts
FROM public.t100_guardian_incident_outbox
WHERE scope_sha256=$1 AND delivered_at IS NULL
ORDER BY seq LIMIT $2
"""
_ATTEMPT = """
UPDATE public.t100_guardian_incident_outbox
SET attempts=attempts+1,last_attempt_at=clock_timestamp()
WHERE scope_sha256=$1 AND seq=$2 AND delivered_at IS NULL
RETURNING attempts
"""
_ACK = """
UPDATE public.t100_guardian_incident_outbox
SET delivered_at=clock_timestamp()
WHERE scope_sha256=$1 AND seq=$2 AND delivered_at IS NULL
RETURNING seq
"""
_COUNT = """
SELECT count(*) FROM public.t100_guardian_incident_outbox
WHERE scope_sha256=$1 AND delivered_at IS NULL
"""


class GuardianIncidentHalted(RuntimeError):
    """Fixed nonsecret failure code. Do not include database exceptions."""


@dataclass(frozen=True)
class GuardianIncidentStage:
    revision: int
    critical_latched: bool
    live_authorization: bool = False


@dataclass(frozen=True)
class PendingGuardianIncident:
    seq: int
    code: str
    severity: str
    attempts: int


class GuardianIncidentOutbox:
    def __init__(self, conn: Any, *, scope_sha256: str):
        if type(scope_sha256) is not str or _SCOPE.fullmatch(scope_sha256) is None:
            raise GuardianIncidentHalted("GUARDIAN_INCIDENT_SCOPE_INVALID")
        if (conn is None or
                not callable(getattr(conn, "fetchrow", None)) or
                not callable(getattr(conn, "fetch", None)) or
                not callable(getattr(conn, "fetchval", None))):
            raise GuardianIncidentHalted("GUARDIAN_INCIDENT_CONNECTION_INVALID")
        self._conn = conn
        self._scope = scope_sha256

    async def acquire_singleton(self) -> None:
        # The advisory lock is held for the lifetime of the isolated
        # alert-writer DB connection. Two Render replicas cannot both
        # claim one account. A different scope requires a separate database.
        try:
            value = await self._conn.fetchval(
                "SELECT pg_try_advisory_lock(110035, 100225)"
            )
        except Exception:
            raise GuardianIncidentHalted("GUARDIAN_INCIDENT_SINGLETON_UNAVAILABLE") from None
        if value is not True:
            raise GuardianIncidentHalted("GUARDIAN_INCIDENT_ALREADY_RUNNING")

    async def observe(self, *, status: str, alert: bool) -> GuardianIncidentStage:
        if (type(status) is not str or _STATUS.fullmatch(status) is None or
                type(alert) is not bool):
            raise GuardianIncidentHalted("GUARDIAN_INCIDENT_STATUS_INVALID")
        try:
            row = await self._conn.fetchrow(_STAGE,self._scope,status,alert)
        except Exception:
            raise GuardianIncidentHalted("GUARDIAN_INCIDENT_STAGE_FAILED") from None
        if (row is None or
                type(row["revision"]) is not int or row["revision"] < 1 or
                type(row["critical_latched"]) is not bool):
            raise GuardianIncidentHalted("GUARDIAN_INCIDENT_COMMIT_UNVERIFIED")
        return GuardianIncidentStage(
            revision=row["revision"],critical_latched=row["critical_latched"])

    async def pending(self, limit: int = 10) -> tuple[PendingGuardianIncident, ...]:
        if type(limit) is not int or not 1 <= limit <= 20:
            raise GuardianIncidentHalted("GUARDIAN_INCIDENT_LIMIT_INVALID")
        try:
            rows = await self._conn.fetch(_PENDING,self._scope,limit)
        except Exception:
            raise GuardianIncidentHalted("GUARDIAN_INCIDENT_PENDING_GET_FAILED") from None
        if not isinstance(rows,(list,tuple)) or len(rows)>limit:
            raise GuardianIncidentHalted("GUARDIAN_INCIDENT_PENDING_UNVERIFIED")
        result=[]
        last=0
        for row in rows:
            try:
                seq,code,severity,attempts=(
                    row["seq"],row["code"],row["severity"],row["attempts"])
            except (TypeError,KeyError,IndexError):
                raise GuardianIncidentHalted("GUARDIAN_INCIDENT_PENDING_UNVERIFIED") from None
            if (type(seq) is not int or seq<=last or
                    type(code) is not str or _STATUS.fullmatch(code) is None or
                    severity not in ("critical","warning") or
                    type(attempts) is not int or attempts<0):
                raise GuardianIncidentHalted("GUARDIAN_INCIDENT_PENDING_UNVERIFIED")
            result.append(PendingGuardianIncident(seq,code,severity,attempts))
            last=seq
        return tuple(result)

    async def mark_attempt(self, seq: int) -> int:
        if type(seq) is not int or seq<1:
            raise GuardianIncidentHalted("GUARDIAN_INCIDENT_SEQ_INVALID")
        try:
            attempts=await self._conn.fetchval(_ATTEMPT,self._scope,seq)
        except Exception:
            raise GuardianIncidentHalted("GUARDIAN_INCIDENT_ATTEMPT_FAILED") from None
        if type(attempts) is not int or attempts<1:
            raise GuardianIncidentHalted("GUARDIAN_INCIDENT_ATTEMPT_UNACKNOWLEDGED")
        return attempts

    async def mark_delivered(self, seq: int) -> None:
        if type(seq) is not int or seq<1:
            raise GuardianIncidentHalted("GUARDIAN_INCIDENT_SEQ_INVALID")
        try:
            val=await self._conn.fetchval(_ACK,self._scope,seq)
        except Exception:
            raise GuardianIncidentHalted("GUARDIAN_INCIDENT_DELIVERY_ACK_FAILED") from None
        if type(val) is not int or val!=seq:
            raise GuardianIncidentHalted("GUARDIAN_INCIDENT_DELIVERY_UNACKNOWLEDGED")

    async def pending_count(self) -> int:
        try:
            n=await self._conn.fetchval(_COUNT,self._scope)
        except Exception:
            raise GuardianIncidentHalted("GUARDIAN_INCIDENT_COUNT_FAILED") from None
        if type(n) is not int or n<0:
            raise GuardianIncidentHalted("GUARDIAN_INCIDENT_COUNT_UNVERIFIED")
        return n
