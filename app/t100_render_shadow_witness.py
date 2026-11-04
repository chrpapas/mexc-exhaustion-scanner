"""Opt-in Render/PostgreSQL witness of the T100 shadow safety kill latch.

No exchange transport, order execution, migrations, or automatic privileges.
A separate one-row table (migration 031) must already exist. Two roles:
writer may INSERT/UPDATE/SELECT ONLY witness; sentinel SELECT ONLY.
The database can persist kill state across worker crash/redeploy unlike
Render's ephemeral files. A successful write is NOT trade authorization.
"""
from __future__ import annotations

import re
import ssl
from dataclasses import dataclass
from typing import Any

from app.t100_live_kill import ALLOWED_REASONS

_SCOPE = re.compile(r"[a-f0-9]{64}\Z")
_RUN = re.compile(r"[a-f0-9]{12,32}\Z")

_WITNESS_READ = """
SELECT scope_sha256, revision, run_id, cycle_index, state, kill_latched,
       kill_reason,
       (extract(epoch from (clock_timestamp() - observed_at)))::double precision AS age_seconds
FROM public.t100_shadow_witness WHERE scope_sha256=$1
"""
# This upsert cannot clear an older latched reason. The database trigger
# separately prevents any UPDATE from clearing a historical kill.
_WITNESS_WRITE = """
INSERT INTO public.t100_shadow_witness
 (scope_sha256, revision, run_id, cycle_index, state,
  kill_latched, kill_reason)
VALUES ($1, 1, $2, $3, $4, $5, $6)
ON CONFLICT (scope_sha256) DO UPDATE SET
 revision = public.t100_shadow_witness.revision + 1,
 run_id = EXCLUDED.run_id,
 cycle_index = EXCLUDED.cycle_index,
 observed_at = clock_timestamp(),
 kill_latched = public.t100_shadow_witness.kill_latched OR EXCLUDED.kill_latched,
 kill_reason = CASE
    WHEN public.t100_shadow_witness.kill_latched
      THEN public.t100_shadow_witness.kill_reason
    ELSE EXCLUDED.kill_reason END,
 state = CASE
    WHEN public.t100_shadow_witness.kill_latched OR EXCLUDED.kill_latched
      THEN 'OBSERVING_KILL_LATCHED'
    ELSE EXCLUDED.state END
RETURNING scope_sha256, revision, run_id, cycle_index, state, kill_latched,
          kill_reason,
          (extract(epoch from (clock_timestamp() - observed_at)))::double precision AS age_seconds
"""


class ShadowWitnessFailure(RuntimeError):
    """Fixed redacted errors; do not expose database paths or payloads."""


@dataclass(frozen=True)
class ShadowWitnessRecord:
    revision: int
    run_id: str
    cycle_index: int
    state: str
    kill_latched: bool
    kill_reason: str | None
    age_seconds: float
    entry_allowed: bool = False
    live_authorization: bool = False
    mexc_orders_sent: bool = False


def parse_record(row: Any, scope: str) -> ShadowWitnessRecord:
    """Strict trust-boundary validation for DB return values."""
    try:
        matched = row["scope_sha256"] == scope
        rev, cycle = row["revision"], row["cycle_index"]
        run = row["run_id"]
        state, latch, reason = row["state"], row["kill_latched"], row["kill_reason"]
        age = row["age_seconds"]
    except (KeyError, TypeError, IndexError):
        raise ShadowWitnessFailure("SHADOW_WITNESS_ROW_INVALID") from None
    if (
        not matched
        or type(rev) is not int or rev < 1
        or type(cycle) is not int or cycle < 0
        or type(run) is not str or _RUN.fullmatch(run) is None
        or type(state) is not str
        or state not in {
            "OBSERVING_KILL_LATCHED", "OBSERVING_NON_AUTHORIZING",
            "SHADOW_RUNTIME_OBSERVATION_FAILED",
        }
        or type(latch) is not bool
        or (latch and reason not in ALLOWED_REASONS)
        or (not latch and reason is not None)
        or (latch and state != "OBSERVING_KILL_LATCHED")
        or type(age) not in (float, int) or not -2.0 <= float(age) <= 315360000
    ):
        raise ShadowWitnessFailure("SHADOW_WITNESS_ROW_INVALID")
    return ShadowWitnessRecord(rev, run, cycle, state, latch, reason, float(age))


class RenderShadowWitness:
    """Persistence adapter accepts an injected asyncpg connection.

    No self-creation of tables, credentials, migrations or trade privileges.
    """

    def __init__(self, conn: Any, *, scope_sha256: str):
        if type(scope_sha256) is not str or not _SCOPE.fullmatch(scope_sha256):
            raise ShadowWitnessFailure("SHADOW_WITNESS_SCOPE_INVALID")
        if conn is None or not callable(getattr(conn, "fetchrow", None)):
            raise ShadowWitnessFailure("SHADOW_WITNESS_CONNECTION_INVALID")
        self._conn = conn
        self._scope = scope_sha256

    async def acquire_singleton(self) -> None:
        """Hold a PostgreSQL *session* advisory lock until conn.close().

        Fail closed instead of having two Render instances interleave
        healthy heartbeats under one scope. It is DB-global for T100.
        """
        try:
            result = await self._conn.fetchval(
                "SELECT pg_try_advisory_lock(110031, 100225)"
            )
        except Exception:  # noqa: BLE001 - redact database connection and query details
            raise ShadowWitnessFailure("SHADOW_WITNESS_LEASE_FAILED") from None
        if result is not True:
            raise ShadowWitnessFailure("SHADOW_WITNESS_WRITER_ALREADY_RUNNING")

    async def restore(self) -> ShadowWitnessRecord | None:
        try:
            row = await self._conn.fetchrow(_WITNESS_READ, self._scope)
        except Exception:  # noqa: BLE001 - redact database connection and query details
            raise ShadowWitnessFailure("SHADOW_WITNESS_READ_FAILED") from None
        if row is None:
            return None  # never promotes this to a healthy observation
        return parse_record(row, self._scope)

    async def record(self, *, run_id: str, cycle_index: int,
                     state: str, kill_latched: bool,
                     kill_reason: str | None) -> ShadowWitnessRecord:
        if (
            type(run_id) is not str or _RUN.fullmatch(run_id) is None
            or type(cycle_index) is not int or cycle_index < 0
            or type(kill_latched) is not bool
            or (kill_latched and kill_reason not in ALLOWED_REASONS)
            or (not kill_latched and kill_reason is not None)
            or state not in {
                "OBSERVING_KILL_LATCHED", "OBSERVING_NON_AUTHORIZING",
                "SHADOW_RUNTIME_OBSERVATION_FAILED",
            }
            or (kill_latched and state != "OBSERVING_KILL_LATCHED")
        ):
            raise ShadowWitnessFailure("SHADOW_WITNESS_INPUT_INVALID")
        try:
            row = await self._conn.fetchrow(
                _WITNESS_WRITE, self._scope, run_id, cycle_index,
                state, kill_latched, kill_reason,
            )
        except Exception:  # noqa: BLE001 - redact database connection and query details
            raise ShadowWitnessFailure("SHADOW_WITNESS_WRITE_FAILED") from None
        if row is None:
            raise ShadowWitnessFailure("SHADOW_WITNESS_WRITE_NOT_ACKNOWLEDGED")
        return parse_record(row, self._scope)


async def connect_render_witness(dsn: str):
    """A single bounded connection. No pool of unnecessary open DB sessions."""
    if type(dsn) is not str or not dsn or not (
        dsn.startswith(("postgresql://", "postgres://"))
    ):
        raise ShadowWitnessFailure("SHADOW_WITNESS_DSN_REQUIRED")
    import asyncpg
    try:
        return await asyncpg.connect(
            dsn=dsn, ssl=ssl.create_default_context(), timeout=5, command_timeout=6
        )
    except Exception:  # noqa: BLE001 - redact database connection and query details
        raise ShadowWitnessFailure("SHADOW_WITNESS_CONNECT_FAILED") from None
