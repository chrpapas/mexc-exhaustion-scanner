"""Restricted, SELECT-only PostgreSQL source for four-slot provenance snapshots.

The caller owns the connection pool. This module never creates a database,
starts a worker, submits a MEXC request or sends SQL writes. Each snapshot
is selected within one verified read-only REPEATABLE READ transaction. A
second snapshot in the coordinator must still match, since exchange GETs
are not an atomic PostgreSQL/MEXC distributed transaction.
"""
from __future__ import annotations

from typing import Any

from app.t100_live_db_inspect import EXPECTED_DATABASE
from app.t100_live_four_slot_snapshot_coordinator import LedgerSnapshot
from app.t100_live_role_preflight import TARGET_ROLE


class FourSlotLedgerReadHalted(RuntimeError):
    pass


CHECKPOINTS_SQL = """
SELECT symbol, external_oid, exchange_order_id, position_id,
       contracts, entry_price, stop_order_id, stop_price,
       best_profit_pct, armed, lifecycle_state, checkpoint_version
FROM public.t100_live_position_checkpoints
WHERE lifecycle_state <> 'CLOSED'
ORDER BY symbol, position_id
"""
REVISIONS_SQL = """
SELECT position_id, checkpoint_version, write_reason, symbol,
       external_oid, exchange_order_id, contracts, entry_price,
       stop_order_id, stop_price, best_profit_pct, armed, lifecycle_state
FROM public.t100_live_position_checkpoint_revisions
WHERE position_id IN (
  SELECT position_id
  FROM public.t100_live_position_checkpoints
  WHERE lifecycle_state <> 'CLOSED'
)
ORDER BY position_id, checkpoint_version
"""
INTENTS_SQL = """
SELECT symbol, external_oid, exchange_order_id,
       volume_contracts, stop_price, state
FROM public.t100_live_order_intents
ORDER BY id
"""
AMENDMENTS_SQL = """
SELECT external_id, symbol, position_id, stop_order_id,
       verified_stop_order_id, previous_stop, proposed_stop, state
FROM public.t100_live_stop_amendments
ORDER BY id
"""


class PostgresFourSlotLedgerReader:
    def __init__(self, pool: Any) -> None:
        if pool is None:
            raise ValueError("FOUR_SLOT_LEDGER_POOL_MISSING")
        self.pool = pool

    async def read_snapshot(self) -> LedgerSnapshot:
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
                        raise FourSlotLedgerReadHalted(
                            "FOUR_SLOT_RESTRICTED_READONLY_CONTEXT_UNVERIFIED"
                        )
                    tables = []
                    for query in (
                        CHECKPOINTS_SQL, REVISIONS_SQL, INTENTS_SQL,
                        AMENDMENTS_SQL,
                    ):
                        records = await conn.fetch(query)
                        if not isinstance(records, (tuple, list)):
                            raise FourSlotLedgerReadHalted(
                                "FOUR_SLOT_LEDGER_QUERY_SHAPE_INVALID"
                            )
                        tables.append([dict(row) for row in records])
                    return LedgerSnapshot(*tables, readonly_repeatable_read=True)
        except FourSlotLedgerReadHalted:
            raise
        except Exception:
            # Do not leak SQL parameters, host, database, or account identifiers.
            raise FourSlotLedgerReadHalted(
                "FOUR_SLOT_LEDGER_READ_FAILED"
            ) from None
