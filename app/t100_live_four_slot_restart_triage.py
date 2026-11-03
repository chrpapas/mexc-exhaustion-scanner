"""Read-only V40 restart triage from a single restricted PostgreSQL snapshot.

This is the preflight inventory for a FUTURE four-slot runtime. It never
starts an order, marks a fill ACK, adopts manual positions, creates native
stops, or repairs ambiguous durable records. A staged or claimed attempt
remains RECONCILE ONLY even when an exact-origin order GET appears empty.

The database's 031/032 migrations are not deployed in production. Missing
tables, privileges, identity drift or excess slots must block, never imply
a clean account or permission to resume exchange POSTs.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from app.t100_live_db_inspect import EXPECTED_DATABASE
from app.t100_live_role_preflight import TARGET_ROLE


ACTIVE_SLOT_RECOVERY_SQL = """
SELECT s.signal_id, s.symbol, s.external_oid, s.state AS slot_state,
       s.holder_id AS slot_holder_id,
       s.fencing_token AS slot_fencing_token,
       i.signal_id AS intent_signal_id, i.symbol AS intent_symbol,
       i.external_oid AS intent_external_oid, i.state AS intent_state,
       c.signal_id AS claim_signal_id,
       c.external_oid AS claim_external_oid,
       c.holder_id AS claim_holder_id,
       c.fencing_token AS claim_fencing_token,
       c.state AS claim_state
FROM public.t100_live_v40_slot_reservations AS s
LEFT JOIN public.t100_live_order_intents AS i
  ON i.signal_id=s.signal_id
LEFT JOIN public.t100_live_v40_dispatch_claims AS c
  ON c.signal_id=s.signal_id
WHERE s.state<>'RELEASED'
ORDER BY s.signal_id
"""


@dataclass(frozen=True)
class RecoveryAttempt:
    signal_id: int
    symbol: str
    external_oid: str
    slot_state: str
    intent_state: str | None
    claim_present: bool
    action: str = "EXACT_ORIGIN_RECONCILIATION_ONLY"
    retry_allowed: bool = False
    manual_position_adopted: bool = False


@dataclass(frozen=True)
class FourSlotRestartTriage:
    status: str
    active_slots: int = 0
    pending_attempts: int = 0
    durable_claims: int = 0
    attempts: tuple[RecoveryAttempt, ...] = ()
    account_inventory_verified: bool = False
    native_stop_protection_verified: bool = False
    manual_position_adopted: bool = False
    retry_allowed: bool = False
    entry_allowed: bool = False
    live_authorization: bool = False
    mexc_orders_sent: bool = False
    database_writes_sent: bool = False
    read_only: bool = True

    def safe_summary(self) -> dict[str, Any]:
        """No IDs, symbols, exchange order data, or credential details."""
        return {
            "status": self.status,
            "active_slots": self.active_slots,
            "pending_attempts": self.pending_attempts,
            "durable_claims": self.durable_claims,
            "account_inventory_verified": False,
            "native_stop_protection_verified": False,
            "manual_position_adopted": False,
            "retry_allowed": False,
            "entry_allowed": False,
            "live_authorization": False,
            "mexc_orders_sent": False,
            "database_writes_sent": False,
            "read_only": True,
        }


class PostgresFourSlotRestartTriage:
    def __init__(self, pool: Any) -> None:
        if pool is None:
            raise ValueError("FOUR_SLOT_RESTART_TRIAGE_POOL_REQUIRED")
        self.pool = pool

    async def inspect(self) -> FourSlotRestartTriage:
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
                        return FourSlotRestartTriage(
                            "RESTART_TRIAGE_RESTRICTED_CONTEXT_UNVERIFIED"
                        )
                    records = await conn.fetch(ACTIVE_SLOT_RECOVERY_SQL)
                    if type(records) not in (tuple, list):
                        return FourSlotRestartTriage(
                            "RESTART_TRIAGE_DB_ROWS_UNVERIFIED"
                        )
                    if len(records) > 4:
                        return FourSlotRestartTriage(
                            "RESTART_TRIAGE_ACCOUNT_OVER_FOUR_SLOTS"
                        )
                    seen_ids: set[int] = set()
                    seen_origins: set[str] = set()
                    seen_symbols: set[str] = set()
                    result: list[RecoveryAttempt] = []
                    claimed = 0
                    for row in records:
                        sid = row["signal_id"]
                        symbol = row["symbol"]
                        origin = row["external_oid"]
                        state = row["slot_state"]
                        holder = row["slot_holder_id"]
                        fence = row["slot_fencing_token"]
                        if (
                            type(sid) is not int or sid <= 0
                            or type(symbol) is not str or not symbol
                            or type(origin) is not str or not origin
                            or type(holder) is not str or not holder
                            or type(fence) is not int or fence <= 0
                            or sid in seen_ids or origin in seen_origins
                            or symbol in seen_symbols
                            or state not in (
                                "RESERVED", "SUBMITTING", "UNKNOWN",
                                "ACK", "PROTECTED", "HALTED",
                            )
                        ):
                            return FourSlotRestartTriage(
                                "RESTART_TRIAGE_SLOT_IDENTITY_DRIFT"
                            )
                        seen_ids.add(sid)
                        seen_origins.add(origin)
                        seen_symbols.add(symbol)
                        istate = row["intent_state"]
                        if state == "RESERVED":
                            # Order intent not yet durably staged.
                            if (
                                row["intent_signal_id"] is not None
                                or row["claim_signal_id"] is not None
                            ):
                                return FourSlotRestartTriage(
                                    "RESTART_TRIAGE_RESERVED_STATE_DRIFT"
                                )
                        elif (
                            row["intent_signal_id"] != sid
                            or row["intent_symbol"] != symbol
                            or row["intent_external_oid"] != origin
                            or istate not in (
                                "SUBMITTING", "UNKNOWN",
                                "ACK", "REVIEW", "CLOSED",
                            )
                        ):
                            return FourSlotRestartTriage(
                                "RESTART_TRIAGE_INTENT_PROVENANCE_DRIFT"
                            )
                        claim_present = row["claim_signal_id"] is not None
                        if claim_present:
                            if (
                                row["claim_signal_id"] != sid
                                or row["claim_external_oid"] != origin
                                or row["claim_holder_id"] != holder
                                or row["claim_fencing_token"] != fence
                                or row["claim_state"]
                                != "POST_POSSIBLE_UNRESOLVED"
                            ):
                                return FourSlotRestartTriage(
                                    "RESTART_TRIAGE_CLAIM_PROVENANCE_DRIFT"
                                )
                            claimed += 1
                        if state in ("ACK", "PROTECTED") and not claim_present:
                            return FourSlotRestartTriage(
                                "RESTART_TRIAGE_PROTECTED_WITHOUT_CLAIM"
                            )
                        if state == "PROTECTED" and istate != "ACK":
                            return FourSlotRestartTriage(
                                "RESTART_TRIAGE_PROTECTED_INTENT_MISMATCH"
                            )
                        if state == "SUBMITTING" and istate != "SUBMITTING":
                            return FourSlotRestartTriage(
                                "RESTART_TRIAGE_STAGED_INTENT_MISMATCH"
                            )
                        result.append(RecoveryAttempt(
                            signal_id=sid, symbol=symbol, external_oid=origin,
                            slot_state=state, intent_state=istate,
                            claim_present=claim_present,
                        ))
                    pending = sum(
                        attempt.slot_state != "PROTECTED"
                        for attempt in result
                    )
                    return FourSlotRestartTriage(
                        status=(
                            "RESTART_TRIAGE_UNRESOLVED_RECONCILE_ONLY"
                            if pending else
                            "RESTART_TRIAGE_NO_PENDING_NONAUTHORIZING"
                        ),
                        active_slots=len(result),
                        pending_attempts=pending,
                        durable_claims=claimed,
                        attempts=tuple(result),
                    )
        except Exception:
            # Never return raw database errors, usernames, hostnames or SQL.
            return FourSlotRestartTriage(
                "RESTART_TRIAGE_DATABASE_READ_FAILED_NO_RETRY"
            )
