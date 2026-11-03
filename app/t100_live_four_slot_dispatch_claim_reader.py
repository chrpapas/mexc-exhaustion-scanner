"""SELECT-only restart witness for immutable V40 dispatch claims.

A committed claim means an exchange POST may already have occurred. This
reader NEVER authorizes a new POST even if an exact-origin order GET returns
nothing; missing or malformed claims are also nonauthorizing. SQL snapshots
must use the restricted executor runtime role in read-only REPEATABLE READ.
No exchange requests, writes, notifications, or credentials are handled here.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Sequence

from app.t100_live_db_inspect import EXPECTED_DATABASE
from app.t100_live_four_slot_attempt_journal import DurableAttemptStage
from app.t100_live_role_preflight import TARGET_ROLE


CLAIM_RECOVERY_SQL = """
SELECT c.signal_id, c.external_oid, c.holder_id,
       c.fencing_token, c.state AS claim_state,
       s.external_oid AS slot_external_oid, s.holder_id AS slot_holder_id,
       s.fencing_token AS slot_fencing_token, s.state AS slot_state,
       i.external_oid AS intent_external_oid, i.state AS intent_state
FROM public.t100_live_v40_dispatch_claims AS c
LEFT JOIN public.t100_live_v40_slot_reservations AS s
  ON s.signal_id = c.signal_id
LEFT JOIN public.t100_live_order_intents AS i
  ON i.signal_id = c.signal_id
WHERE c.signal_id = ANY($1::bigint[])
ORDER BY c.signal_id
"""


@dataclass(frozen=True)
class DispatchClaimRecovery:
    status: str
    claimed_attempts: int = 0
    all_claims_exact_and_durable: bool = False
    reconciliation_required: bool = True
    resubmission_allowed: bool = False
    entry_allowed: bool = False
    live_authorization: bool = False
    mexc_orders_sent: bool = False
    read_only: bool = True


class DispatchClaimRecoveryHalted(RuntimeError):
    pass


class PostgresFourSlotDispatchClaimReader:
    """Never trust admin/privileged or transaction-read-write context."""

    def __init__(self, pool: Any) -> None:
        if pool is None:
            raise ValueError("DISPATCH_RECOVERY_POOL_REQUIRED")
        self.pool = pool

    async def inspect(
        self, stages: Sequence[DurableAttemptStage],
    ) -> DispatchClaimRecovery:
        if (
            type(stages) not in (tuple, list)
            or not 1 <= len(stages) <= 4
            or any(type(s) is not DurableAttemptStage for s in stages)
            or any(
                type(s.signal_id) is not int or s.signal_id <= 0
                or type(s.external_oid) is not str or not s.external_oid
                or s.state != "SUBMITTING"
                or s.post_attempted is not False
                or s.reconciliation_required is not True
                or s.entry_allowed is not False
                or s.live_authorization is not False
                for s in stages
            )
            or len({s.signal_id for s in stages}) != len(stages)
            or len({s.external_oid for s in stages}) != len(stages)
        ):
            raise DispatchClaimRecoveryHalted(
                "DISPATCH_RECOVERY_STAGED_IDENTITIES_INVALID"
            )
        wanted = {s.signal_id: s.external_oid for s in stages}
        try:
            async with self.pool.acquire() as conn:
                async with conn.transaction(
                    isolation="repeatable_read", readonly=True,
                ):
                    if (
                        await conn.fetchval(
                            "SELECT current_setting('transaction_read_only')"
                        ) != "on"
                        or await conn.fetchval(
                            "SELECT current_setting('transaction_isolation')"
                        ) != "repeatable read"
                        or await conn.fetchval("SELECT current_database()")
                        != EXPECTED_DATABASE
                        or await conn.fetchval("SELECT current_user")
                        != TARGET_ROLE
                    ):
                        raise DispatchClaimRecoveryHalted(
                            "DISPATCH_RECOVERY_RESTRICTED_CONTEXT_REQUIRED"
                        )
                    rows = await conn.fetch(
                        CLAIM_RECOVERY_SQL, sorted(wanted),
                    )
                    if type(rows) not in (list, tuple):
                        raise DispatchClaimRecoveryHalted(
                            "DISPATCH_RECOVERY_ROWS_INVALID"
                        )
                    # No missing claim can prove that a POST did NOT happen:
                    # treat absence as nonauthorizing and require manual
                    # review, never try again.
                    if len(rows) != len(stages):
                        return DispatchClaimRecovery(
                            status="DISPATCH_CLAIMS_MISSING_NO_RETRY",
                            claimed_attempts=len(rows),
                        )
                    seen: set[int] = set()
                    for row in rows:
                        sid = row["signal_id"]
                        if (
                            type(sid) is not int
                            or sid in seen
                            or sid not in wanted
                            or row["external_oid"] != wanted[sid]
                            or row["claim_state"] != "POST_POSSIBLE_UNRESOLVED"
                            or type(row["fencing_token"]) is not int
                            or row["fencing_token"] <= 0
                            or type(row["holder_id"]) is not str
                            or not row["holder_id"]
                            or row["slot_external_oid"] != wanted[sid]
                            or row["intent_external_oid"] != wanted[sid]
                            or row["slot_holder_id"] != row["holder_id"]
                            or row["slot_fencing_token"] != row["fencing_token"]
                            or row["slot_state"] not in (
                                "SUBMITTING", "UNKNOWN", "ACK",
                                "PROTECTED", "HALTED", "RELEASED",
                            )
                            or row["intent_state"] not in (
                                "SUBMITTING", "UNKNOWN", "ACK",
                                "REVIEW", "CLOSED",
                            )
                        ):
                            return DispatchClaimRecovery(
                                status="DISPATCH_CLAIM_JOURNAL_DRIFT_NO_RETRY",
                                claimed_attempts=len(rows),
                            )
                        seen.add(sid)
                    return DispatchClaimRecovery(
                        status="DISPATCH_CLAIMS_PRESENT_RECONCILE_ONLY",
                        claimed_attempts=len(rows),
                        all_claims_exact_and_durable=True,
                    )
        except DispatchClaimRecoveryHalted:
            raise
        except Exception:
            raise DispatchClaimRecoveryHalted(
                "DISPATCH_RECOVERY_DATABASE_READ_FAILED_NO_RETRY"
            ) from None
