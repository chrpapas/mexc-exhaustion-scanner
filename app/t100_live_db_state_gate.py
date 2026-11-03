"""Read-only unresolved live-state gate for T100.

A live mutation permit must not be issued while the durable journals contain
an unresolved exchange outcome or an intervention-required lifecycle state.
"""
from __future__ import annotations

from typing import Any


async def assess_unresolved_live_state(pool: Any) -> dict[str, Any]:
    result: dict[str, Any] = {
        "prepared_order_intents": 0,
        "submitting_order_intents": 0,
        "unknown_order_intents": 0,
        "unresolved_stop_amendments": 0,
        "review_checkpoints": 0,
        "closing_checkpoints": 0,
        "unresolved_mutations_absent": False,
        "live_authorization": False,
    }
    if pool is None:
        result["status"] = "LIVE_STATE_POOL_MISSING"
        return result
    try:
        row = await pool.fetchrow(
            """SELECT
                 (SELECT count(*) FROM t100_live_order_intents
                   WHERE state='PREPARED') AS prepared_order_intents,
                 (SELECT count(*) FROM t100_live_order_intents
                   WHERE state='SUBMITTING') AS submitting_order_intents,
                 (SELECT count(*) FROM t100_live_order_intents
                   WHERE state='UNKNOWN') AS unknown_order_intents,
                 (SELECT count(*) FROM t100_live_stop_amendments
                   WHERE state <> 'VERIFIED') AS unresolved_stop_amendments,
                 (SELECT count(*) FROM t100_live_position_checkpoints
                   WHERE lifecycle_state='REVIEW') AS review_checkpoints,
                 (SELECT count(*) FROM t100_live_position_checkpoints
                   WHERE lifecycle_state='CLOSING') AS closing_checkpoints"""
        )
    except Exception as exc:
        result["status"] = "LIVE_STATE_READ_FAILED"
        result["failure_type"] = type(exc).__name__
        return result
    if row is None:
        result["status"] = "LIVE_STATE_RESPONSE_MISSING"
        return result

    fields = (
        "prepared_order_intents",
        "submitting_order_intents",
        "unknown_order_intents",
        "unresolved_stop_amendments",
        "review_checkpoints",
        "closing_checkpoints",
    )
    counts: dict[str, int] = {}
    for field in fields:
        value = row.get(field)
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            result["status"] = "LIVE_STATE_COUNT_INVALID"
            return result
        counts[field] = value
    result.update(counts)
    clean = all(value == 0 for value in counts.values())
    result["unresolved_mutations_absent"] = clean
    result["status"] = (
        "NO_UNRESOLVED_LIVE_MUTATIONS_OR_INTERVENTION_STATES"
        if clean
        else "UNRESOLVED_LIVE_MUTATION_OR_INTERVENTION_STATE"
    )
    return result
