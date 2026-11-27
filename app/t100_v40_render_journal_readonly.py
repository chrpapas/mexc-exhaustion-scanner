"""Independent V40 first-position dispatch journal inspection, GET/SELECT only.

The scanner signal-reader role cannot read or write execution journals.
This optional operator-configured gate uses ONLY the pre-existing restricted
t100_live_executor_runtime login via T100_V40_EXECUTOR_RUNTIME_DSN, under a
repeatable-read READ ONLY transaction. There is no INSERT, UPDATE, DELETE,
DDL, MEXC request, key access, stop manipulation or order authorization.

The first-position live pilot MUST see no existing V40 slot/claim of any
state, and no unresolved legacy live-order attempt. An old completed,
protected or released V40 claim is not silently reset/reused here.
This gate reports only a snapshot, not future transaction atomicity.
"""
from __future__ import annotations

import asyncio
from dataclasses import dataclass, asdict
import json
import os
from typing import Any, Mapping

from app.t100_live_db_inspect import EXPECTED_DATABASE
from app.t100_live_role_preflight import TARGET_ROLE
from app.t100_live_v40_state_gate import assess_v40_unresolved_dispatch_state

DSN_KEY="T100_V40_EXECUTOR_RUNTIME_DSN"
BASE_UNRESOLVED_SQL="""
SELECT
  (SELECT count(*) FROM public.t100_live_order_intents
   WHERE state IN ('PREPARED','SUBMITTING','UNKNOWN','REVIEW')) AS open_intents,
  (SELECT count(*) FROM public.t100_live_stop_amendments
   WHERE state <> 'VERIFIED') AS unresolved_stop_updates,
  (SELECT count(*) FROM public.t100_live_vol30_parents
   WHERE status IN ('ACTIVE','HALTED')) AS open_legacy_parents,
  (SELECT count(*) FROM public.t100_live_vol30_children
   WHERE state IN ('PREPARED','SUBMITTING','UNKNOWN','ACK','PARTIAL','HALTED'))
    AS open_legacy_children
"""
BASE_FIELDS=(
    "open_intents","unresolved_stop_updates",
    "open_legacy_parents","open_legacy_children",
)


@dataclass(frozen=True)
class V40RuntimeJournalObservation:
    status: str
    runtime_role_verified: bool = False
    repeatable_read_readonly_verified: bool = False
    v40_tables_observed: bool = False
    v40_dispatch_state_unresolved_absent: bool = False
    v40_no_prior_claims_or_slots: bool = False
    legacy_unresolved_absent: bool = False
    first_position_journal_clear: bool = False
    entry_allowed: bool = False
    live_authorization: bool = False
    mexc_posts: int = 0
    database_writes: int = 0


async def inspect_runtime_journal(conn:Any)->V40RuntimeJournalObservation:
    """Only call INSIDE a caller-enforced read-only repeatable-read tx."""
    def blocked(code):
        return V40RuntimeJournalObservation(status=code)
    try:
        if not (
            await conn.fetchval("SELECT current_database()")==EXPECTED_DATABASE
            and await conn.fetchval("SELECT current_user")==TARGET_ROLE
            and await conn.fetchval(
                "SELECT current_setting('transaction_read_only')"
            )=="on"
            and await conn.fetchval(
                "SELECT current_setting('transaction_isolation')"
            )=="repeatable read"
        ):
            return blocked("V40_RUNTIME_JOURNAL_ROLE_OR_READONLY_CONTEXT_HALT")
        state=await assess_v40_unresolved_dispatch_state(conn)
        if not (
            type(state) is dict
            and state.get("status")=="V40_DB_STATE_CLEAR_NONAUTHORIZING"
            and state.get("v40_slot_table_observed") is True
            and state.get("v40_dispatch_claim_table_observed") is True
            and state.get("v40_unresolved_dispatch_state_absent") is True
        ):
            return blocked("V40_RUNTIME_JOURNAL_UNRESOLVED_OR_SCHEMA_HALT")
        # First pilot starts with NO history of one-shot claims, even if
        # older states were marked PROTECTED or RELEASED. No reuse.
        if not (
            state.get("total_dispatch_claims")==0
            and state.get("protected_reservations")==0
        ):
            return blocked("V40_RUNTIME_JOURNAL_PRIOR_CLAIM_REVIEW_REQUIRED")
        slots=await conn.fetchval(
            "SELECT count(*) FROM public.t100_live_v40_slot_reservations"
        )
        if type(slots) is not int or slots!=0:
            return blocked("V40_RUNTIME_JOURNAL_PRIOR_SLOT_REVIEW_REQUIRED")
        legacy=await conn.fetchrow(BASE_UNRESOLVED_SQL)
        if (
            legacy is None
            or any(type(legacy[f]) is not int or legacy[f]!=0
                   for f in BASE_FIELDS)
        ):
            return blocked("V40_RUNTIME_JOURNAL_BASE_MUTATIONS_UNRESOLVED")
        return V40RuntimeJournalObservation(
            status="V40_FIRST_POSITION_JOURNAL_EMPTY_AND_READONLY_VERIFIED",
            runtime_role_verified=True,
            repeatable_read_readonly_verified=True,
            v40_tables_observed=True,
            v40_dispatch_state_unresolved_absent=True,
            v40_no_prior_claims_or_slots=True,
            legacy_unresolved_absent=True,
            first_position_journal_clear=True,
        )
    except Exception:
        return blocked("V40_RUNTIME_JOURNAL_QUERY_OR_PRIVILEGE_HALT")


async def observe_render_runtime_journal(env:Mapping[str,str])->V40RuntimeJournalObservation:
    dsn=env.get(DSN_KEY,"")
    if (
        type(dsn) is not str or
        not dsn.startswith(("postgresql://","postgres://"))
    ):
        return V40RuntimeJournalObservation(
            "V40_RUNTIME_JOURNAL_RESTRICTED_EXECUTOR_DSN_MISSING"
        )
    # Do not import asyncpg, connect or read other process credentials
    # before the explicit separate runtime DSN has been configured.
    try:
        import asyncpg
        conn=await asyncpg.connect(
            dsn=dsn,ssl="require",timeout=10,command_timeout=15,
        )
        try:
            async with conn.transaction(isolation="repeatable_read",readonly=True):
                return await inspect_runtime_journal(conn)
        finally:
            await conn.close()
    except Exception:
        return V40RuntimeJournalObservation(
            "V40_RUNTIME_JOURNAL_CONNECT_OR_TRANSACTION_HALT"
        )


def main()->int:
    report=asyncio.run(observe_render_runtime_journal(dict(os.environ)))
    print(json.dumps(asdict(report),sort_keys=True))
    return 0 if report.first_position_journal_clear else 2


if __name__=="__main__":
    raise SystemExit(main())
