"""One-shot read-only forensic summary of ALL V40 guardian incident generations.

SELECT-only under a repeatable-read transaction. Never rewrites/clears
incident state, never creates another scope, never grants entry permission.
Output contains only fixed scope labels, boolean fields, and integer totals.
No raw SQL rows, exchange IDs, credentials or incident message bodies.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Any

from app.t100_v40_guardian_reviewed_generation import (
    LEGACY_SCOPE,REVIEWED_SCOPE,RECOVERY_SCOPE_002,
    GOOD_STATUS,HISTORIC_CRITICAL,
)
from app.t100_v40_guardian_recovery_003 import RECOVERY_SCOPE_003


class GuardianArchiveAuditHalted(RuntimeError):
    pass


_SCOPES=(
    ("legacy",LEGACY_SCOPE),
    ("reviewed_001",REVIEWED_SCOPE),
    ("recovery_002",RECOVERY_SCOPE_002),
    ("recovery_003",RECOVERY_SCOPE_003),
)


@dataclass(frozen=True)
class GuardianArchiveScopeAudit:
    name: str
    state_exists: bool
    last_observation_flat: bool
    recent_120s: bool
    last_alert: bool | None
    critical_latched: bool | None
    revision: int | None
    critical_total: int
    critical_get_only_class_count: int
    critical_other_class_count: int
    undelivered_total: int


@dataclass(frozen=True)
class GuardianArchiveAudit:
    status: str
    scopes: tuple[GuardianArchiveScopeAudit,...]
    all_scopes_present: bool
    every_critical_alert_delivered: bool
    active_scope_003_critical_latched: bool
    active_scope_003_currently_flat: bool
    incident_history_verified: bool = False
    live_authorization: bool = False
    entry_allowed: bool = False
    mexc_posts: int = 0
    database_writes: int = 0

    def safe_json(self) -> dict[str,Any]:
        from dataclasses import asdict
        return asdict(self)


async def audit_guardian_archive_readonly(conn: Any) -> GuardianArchiveAudit:
    """Check exact immutable database scope facts; report no unsafe raw data."""
    if conn is None or not callable(getattr(conn,"transaction",None)):
        raise GuardianArchiveAuditHalted("V40_ARCHIVE_AUDIT_READONLY_CONNECTION_UNVERIFIED")
    found=[]
    try:
        async with conn.transaction(isolation="repeatable_read",readonly=True):
            if await conn.fetchval("SELECT current_setting('transaction_read_only')")!="on":
                raise GuardianArchiveAuditHalted("V40_ARCHIVE_AUDIT_TRANSACTION_NOT_READONLY")
            for name,scope in _SCOPES:
                row=await conn.fetchrow(
                    """SELECT last_status,last_alert,critical_latched,revision,
                        observed_at >= clock_timestamp() - interval '120 seconds' AS recent
                       FROM public.t100_guardian_incident_state
                       WHERE scope_sha256=$1""",scope,
                )
                counts=await conn.fetchrow(
                    """SELECT
                         COUNT(*) FILTER (WHERE severity='critical')::int AS critical_total,
                         COUNT(*) FILTER (
                             WHERE severity='critical' AND code=$2
                         )::int AS benign_class_count,
                         COUNT(*) FILTER (
                             WHERE severity='critical' AND code<>$2
                         )::int AS other_class_count,
                         COUNT(*) FILTER (WHERE delivered_at IS NULL)::int AS undelivered
                       FROM public.t100_guardian_incident_outbox
                       WHERE scope_sha256=$1""",scope,HISTORIC_CRITICAL,
                )
                if counts is None:
                    raise GuardianArchiveAuditHalted("V40_ARCHIVE_AUDIT_COUNTS_UNAVAILABLE")
                metrics=tuple(counts[k] for k in (
                    "critical_total","benign_class_count","other_class_count","undelivered"
                ))
                if any(type(n) is not int or n<0 for n in metrics):
                    raise GuardianArchiveAuditHalted("V40_ARCHIVE_AUDIT_COUNTS_INVALID")
                if metrics[0]!=metrics[1]+metrics[2]:
                    raise GuardianArchiveAuditHalted("V40_ARCHIVE_AUDIT_COUNTS_INCONSISTENT")
                if row is None:
                    found.append(GuardianArchiveScopeAudit(
                        name,False,False,False,None,None,None,*metrics
                    ))
                    continue
                if (
                    type(row["last_status"]) is not str
                    or type(row["last_alert"]) is not bool
                    or type(row["critical_latched"]) is not bool
                    or type(row["revision"]) is not int
                    or row["revision"] < 1
                    or type(row["recent"]) is not bool
                ):
                    raise GuardianArchiveAuditHalted("V40_ARCHIVE_AUDIT_STATE_INVALID")
                found.append(GuardianArchiveScopeAudit(
                    name,True,row["last_status"]==GOOD_STATUS,
                    row["recent"],row["last_alert"],row["critical_latched"],
                    row["revision"],*metrics,
                ))
    except GuardianArchiveAuditHalted:
        raise
    except Exception:
        raise GuardianArchiveAuditHalted("V40_ARCHIVE_AUDIT_SELECT_UNAVAILABLE") from None
    if len(found)!=4:
        raise GuardianArchiveAuditHalted("V40_ARCHIVE_AUDIT_SCOPE_COUNT_INVALID")
    active=found[-1]
    return GuardianArchiveAudit(
        status="V40_GUARDIAN_ARCHIVE_READONLY_AUDIT_COMPLETE_NONAUTHORIZING",
        scopes=tuple(found),
        all_scopes_present=all(s.state_exists for s in found),
        every_critical_alert_delivered=all(s.undelivered_total==0 for s in found),
        active_scope_003_critical_latched=active.critical_latched is True,
        active_scope_003_currently_flat=active.last_observation_flat,
        incident_history_verified=False,
    )
