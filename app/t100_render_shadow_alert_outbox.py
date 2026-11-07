"""Durable, transactionally staged Render/PG sentinel Discord alert outbox.

No MEXC credentials or order transport. Requires pre-reviewed migration 032
and a dedicated least-privilege sentinel DB role. Never creates tables,
clears alarms, acknowledges human intervention or authorizes entries.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

_SCOPE = re.compile(r"[a-f0-9]{64}\Z")
_STATUS = re.compile(r"SHADOW_WITNESS_[A-Z0-9_]{1,64}\Z")

_STAGE = """
INSERT INTO public.t100_shadow_sentinel_state
 (scope_sha256,revision,last_status,last_alert)
VALUES ($1,1,$2,$3)
ON CONFLICT (scope_sha256) DO UPDATE SET
 revision = public.t100_shadow_sentinel_state.revision + 1,
 last_status = EXCLUDED.last_status,
 last_alert = EXCLUDED.last_alert,
 observed_at = clock_timestamp()
RETURNING revision
"""
_PENDING = """
SELECT seq,code,attempts
FROM public.t100_shadow_sentinel_outbox
WHERE scope_sha256=$1 AND delivered_at IS NULL
ORDER BY seq LIMIT $2
"""
_ATTEMPT = """
UPDATE public.t100_shadow_sentinel_outbox SET
 attempts=attempts+1,last_attempt_at=clock_timestamp()
WHERE scope_sha256=$1 AND seq=$2 AND delivered_at IS NULL
RETURNING attempts
"""
_ACK = """
UPDATE public.t100_shadow_sentinel_outbox SET
 delivered_at=clock_timestamp()
WHERE scope_sha256=$1 AND seq=$2 AND delivered_at IS NULL
RETURNING seq
"""
_COUNT = """
SELECT count(*) FROM public.t100_shadow_sentinel_outbox
WHERE scope_sha256=$1 AND delivered_at IS NULL
"""


class RenderAlertOutboxFailure(RuntimeError):
    """Only fixed, low-cardinality failure codes; never expose raw SQL errors."""


@dataclass(frozen=True)
class PendingRenderAlert:
    seq: int
    code: str
    attempts: int


class RenderShadowAlertOutbox:
    """Owns one PostgreSQL connection; singleton lease lasts until conn.close."""

    def __init__(self, conn: Any, *, scope_sha256: str):
        if type(scope_sha256) is not str or _SCOPE.fullmatch(scope_sha256) is None:
            raise RenderAlertOutboxFailure("RENDER_OUTBOX_SCOPE_INVALID")
        if (
            conn is None
            or not callable(getattr(conn, "fetchrow", None))
            or not callable(getattr(conn, "fetch", None))
            or not callable(getattr(conn, "fetchval", None))
        ):
            raise RenderAlertOutboxFailure("RENDER_OUTBOX_CONNECTION_INVALID")
        self._conn = conn
        self._scope = scope_sha256

    async def acquire_singleton(self) -> None:
        try:
            acquired = await self._conn.fetchval(
                "SELECT pg_try_advisory_lock(110032, 100225)"
            )
        except Exception:
            raise RenderAlertOutboxFailure("RENDER_OUTBOX_LEASE_FAILED") from None
        if acquired is not True:
            raise RenderAlertOutboxFailure("RENDER_OUTBOX_SENTINEL_ALREADY_RUNNING")

    async def observe(self, *, code: str, alert: bool) -> int:
        if (
            type(code) is not str or _STATUS.fullmatch(code) is None
            or type(alert) is not bool
        ):
            raise RenderAlertOutboxFailure("RENDER_OUTBOX_STATUS_INVALID")
        try:
            rev = await self._conn.fetchval(_STAGE, self._scope, code, alert)
        except Exception:
            raise RenderAlertOutboxFailure("RENDER_OUTBOX_STAGE_FAILED") from None
        if type(rev) is not int or rev < 1:
            raise RenderAlertOutboxFailure("RENDER_OUTBOX_STAGE_UNACKNOWLEDGED")
        return rev

    async def pending(self, *, limit: int = 10) -> tuple[PendingRenderAlert, ...]:
        if type(limit) is not int or not 1 <= limit <= 20:
            raise RenderAlertOutboxFailure("RENDER_OUTBOX_LIMIT_INVALID")
        try:
            rows = await self._conn.fetch(_PENDING, self._scope, limit)
        except Exception:
            raise RenderAlertOutboxFailure("RENDER_OUTBOX_PENDING_READ_FAILED") from None
        if not isinstance(rows, (list, tuple)) or len(rows) > limit:
            raise RenderAlertOutboxFailure("RENDER_OUTBOX_PENDING_INVALID")
        result = []
        last_seq = 0
        for row in rows:
            try:
                seq, code, attempts = row["seq"], row["code"], row["attempts"]
            except (KeyError, TypeError, IndexError):
                raise RenderAlertOutboxFailure("RENDER_OUTBOX_PENDING_INVALID") from None
            if (
                type(seq) is not int or seq <= last_seq
                or type(code) is not str or _STATUS.fullmatch(code) is None
                or type(attempts) is not int or attempts < 0
            ):
                raise RenderAlertOutboxFailure("RENDER_OUTBOX_PENDING_INVALID")
            result.append(PendingRenderAlert(seq, code, attempts))
            last_seq = seq
        return tuple(result)

    async def mark_attempt(self, seq: int) -> int:
        if type(seq) is not int or seq < 1:
            raise RenderAlertOutboxFailure("RENDER_OUTBOX_ALERT_ID_INVALID")
        try:
            attempts = await self._conn.fetchval(_ATTEMPT, self._scope, seq)
        except Exception:
            raise RenderAlertOutboxFailure("RENDER_OUTBOX_ATTEMPT_WRITE_FAILED") from None
        if type(attempts) is not int or attempts < 1:
            raise RenderAlertOutboxFailure("RENDER_OUTBOX_ATTEMPT_UNACKNOWLEDGED")
        return attempts

    async def mark_delivered(self, seq: int) -> None:
        if type(seq) is not int or seq < 1:
            raise RenderAlertOutboxFailure("RENDER_OUTBOX_ALERT_ID_INVALID")
        try:
            acknowledged = await self._conn.fetchval(_ACK, self._scope, seq)
        except Exception:
            raise RenderAlertOutboxFailure("RENDER_OUTBOX_ACK_WRITE_FAILED") from None
        if acknowledged != seq or type(acknowledged) is not int:
            raise RenderAlertOutboxFailure("RENDER_OUTBOX_ACK_UNACKNOWLEDGED")

    async def count_pending(self) -> int:
        try:
            count = await self._conn.fetchval(_COUNT, self._scope)
        except Exception:
            raise RenderAlertOutboxFailure("RENDER_OUTBOX_PENDING_COUNT_FAILED") from None
        if type(count) is not int or count < 0:
            raise RenderAlertOutboxFailure("RENDER_OUTBOX_PENDING_COUNT_INVALID")
        return count
