"""Read-only V40 reservation/dispatch ambiguity gate.

The legacy live-state report covers migrations 027-030 but NOT V40
reservations or irreversible dispatch claims (031/032). This separate
observer is intentionally fail-closed when either V40 table is missing,
inaccessible or inconsistent. It is a state *inventory*, not venue,
schema-privilege, collateral, protected-soak, or authorization proof.

There are no migrations, DB mutations, exchange calls or credentials here.
"""
from __future__ import annotations

from typing import Any

SCHEMA_SQL = """
SELECT
    to_regclass('public.t100_live_v40_slot_reservations') IS NOT NULL
        AS slots_present,
    to_regclass('public.t100_live_v40_dispatch_claims') IS NOT NULL
        AS claims_present
"""

COUNTS_SQL = """
SELECT
    (SELECT count(*) FROM public.t100_live_v40_slot_reservations
     WHERE state IN ('RESERVED','SUBMITTING','UNKNOWN','ACK','HALTED'))
        AS unresolved_reservations,
    (SELECT count(*)
     FROM public.t100_live_v40_dispatch_claims AS c
     JOIN public.t100_live_v40_slot_reservations AS s
       ON s.signal_id=c.signal_id
     WHERE s.state NOT IN ('PROTECTED','RELEASED'))
        AS unresolved_dispatch_claims,
    (SELECT count(*)
     FROM public.t100_live_v40_slot_reservations AS s
     WHERE s.state='PROTECTED' AND NOT EXISTS (
         SELECT 1 FROM public.t100_live_v40_dispatch_claims AS c
         WHERE c.signal_id=s.signal_id
           AND c.external_oid=s.external_oid
     ))
        AS unclaimed_protected_reservations,
    (SELECT count(*)
     FROM public.t100_live_v40_dispatch_claims AS c
     LEFT JOIN public.t100_live_v40_slot_reservations AS s
       ON s.signal_id=c.signal_id
     LEFT JOIN public.t100_live_order_intents AS i
       ON i.signal_id=c.signal_id
     WHERE s.signal_id IS NULL OR i.signal_id IS NULL
        OR s.external_oid<>c.external_oid
        OR i.external_oid<>c.external_oid
        OR s.holder_id<>c.holder_id
        OR s.fencing_token<>c.fencing_token
        OR c.state<>'POST_POSSIBLE_UNRESOLVED'
        OR (s.state='PROTECTED' AND i.state<>'ACK')
        OR (s.state='RELEASED' AND i.state<>'CLOSED')
    ))
        AS dispatch_claim_provenance_mismatches,
    (SELECT count(*) FROM public.t100_live_v40_slot_reservations
     WHERE state='PROTECTED')
        AS protected_reservations,
    (SELECT count(*) FROM public.t100_live_v40_dispatch_claims)
        AS total_dispatch_claims
"""


FIELDS = (
    "unresolved_reservations",
    "unresolved_dispatch_claims",
    "unclaimed_protected_reservations",
    "dispatch_claim_provenance_mismatches",
    "protected_reservations",
    "total_dispatch_claims",
)
BLOCKERS = FIELDS[:4]


async def assess_v40_unresolved_dispatch_state(pool: Any) -> dict[str, Any]:
    """Conservative classification of durable V40 ambiguity only."""
    result: dict[str, Any] = {
        **{k: None for k in FIELDS},
        "v40_slot_table_observed": False,
        "v40_dispatch_claim_table_observed": False,
        "v40_unresolved_dispatch_state_absent": False,
        "entry_allowed": False,
        "live_authorization": False,
        "mexc_orders_sent": False,
        "read_only": True,
    }
    if pool is None:
        result["status"] = "V40_DB_STATE_POOL_MISSING"
        return result
    try:
        schema = await pool.fetchrow(SCHEMA_SQL)
        if type(schema) is not dict:
            result["status"] = "V40_DB_STATE_SCHEMA_READ_UNVERIFIED"
            return result
        result["v40_slot_table_observed"] = (
            schema.get("slots_present") is True
        )
        result["v40_dispatch_claim_table_observed"] = (
            schema.get("claims_present") is True
        )
        if not (
            result["v40_slot_table_observed"]
            and result["v40_dispatch_claim_table_observed"]
        ):
            result["status"] = "V40_DB_STATE_MIGRATIONS_NOT_OBSERVED"
            return result
        row = await pool.fetchrow(COUNTS_SQL)
        if type(row) is not dict:
            result["status"] = "V40_DB_STATE_COUNTS_MISSING"
            return result
        if any(type(row.get(field)) is not int or row[field] < 0
               for field in FIELDS):
            result["status"] = "V40_DB_STATE_COUNTS_INVALID"
            return result
        for field in FIELDS:
            result[field] = row[field]
        clear = all(row[field] == 0 for field in BLOCKERS)
        result["v40_unresolved_dispatch_state_absent"] = clear
        result["status"] = (
            "V40_DB_STATE_CLEAR_NONAUTHORIZING" if clear
            else "V40_DB_DISPATCH_OR_RESERVATION_UNRESOLVED"
        )
        return result
    except Exception:
        result["status"] = "V40_DB_STATE_READ_FAILED"
        return result
