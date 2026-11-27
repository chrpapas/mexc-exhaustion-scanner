"""Dedicated V40 guardian ledger reader: SELECT-only, different DB role.

The active T100 executor runtime role can WRITE order and checkpoint
journals; the independent risk guardian must NEVER reuse that credential.
This reader requires the exact dedicated guardian role, REPEATABLE READ,
read-only transaction and strictly SELECT-only privileges on the four
position/intent/amendment tables before returning immutable provenance.
No account enrollment by API key fingerprint is needed in this V40-only
deployment: ownership is proven via exact immutable checkpoint, filled
entry order and both MEXC signed account bookends.

Provision this role manually after review. No grants/role creation here.
"""
from __future__ import annotations

from typing import Any

from app.t100_live_db_inspect import EXPECTED_DATABASE
from app.t100_live_four_slot_ledger_reader import (
    CHECKPOINTS_SQL, REVISIONS_SQL, INTENTS_SQL, AMENDMENTS_SQL,
)
from app.t100_live_four_slot_snapshot_coordinator import LedgerSnapshot

V40_GUARDIAN_READER_ROLE="t100_v40_guardian_reader"


class V40GuardianLedgerHalted(RuntimeError):
    pass


_TABLES=(
    "t100_live_position_checkpoints",
    "t100_live_position_checkpoint_revisions",
    "t100_live_order_intents",
    "t100_live_stop_amendments",
)
_PRIV_SQL="""
SELECT has_table_privilege(current_user,
            'public.'||name,'SELECT') AS can_select,
       has_table_privilege(current_user,
            'public.'||name,'INSERT,UPDATE,DELETE,TRUNCATE,REFERENCES,TRIGGER')
            AS can_mutate
FROM unnest($1::text[]) AS name
"""


class PostgresV40GuardianLedgerReader:
    def __init__(self,pool:Any):
        if pool is None:
            raise V40GuardianLedgerHalted("V40_GUARDIAN_POOL_MISSING")
        self.pool=pool

    async def read_snapshot(self)->LedgerSnapshot:
        try:
            async with self.pool.acquire() as conn:
                async with conn.transaction(
                    isolation="repeatable_read",readonly=True,
                ):
                    role=await conn.fetchval("SELECT current_user")
                    db=await conn.fetchval("SELECT current_database()")
                    isolation=await conn.fetchval(
                        "SELECT current_setting('transaction_isolation')"
                    )
                    read_only=await conn.fetchval(
                        "SELECT current_setting('transaction_read_only')"
                    )
                    if not (
                        role==V40_GUARDIAN_READER_ROLE
                        and db==EXPECTED_DATABASE
                        and isolation=="repeatable read"
                        and read_only=="on"
                    ):
                        raise V40GuardianLedgerHalted(
                            "V40_GUARDIAN_RESTRICTED_DB_ROLE_OR_READONLY_REQUIRED"
                        )
                    grants=await conn.fetch(_PRIV_SQL,list(_TABLES))
                    if (
                        type(grants) not in (list,tuple)
                        or len(grants)!=len(_TABLES)
                        or any(
                            row["can_select"] is not True
                            or row["can_mutate"] is not False
                            for row in grants
                        )
                    ):
                        raise V40GuardianLedgerHalted(
                            "V40_GUARDIAN_READONLY_TABLE_GRANTS_INVALID"
                        )
                    tables=[]
                    for statement in (
                        CHECKPOINTS_SQL,REVISIONS_SQL,INTENTS_SQL,
                        AMENDMENTS_SQL,
                    ):
                        rows=await conn.fetch(statement)
                        if type(rows) not in (list,tuple):
                            raise V40GuardianLedgerHalted(
                                "V40_GUARDIAN_LEDGER_ROWS_INVALID"
                            )
                        tables.append([dict(r) for r in rows])
                    return LedgerSnapshot(
                        *tables,readonly_repeatable_read=True,
                    )
        except V40GuardianLedgerHalted:
            raise
        except Exception:
            raise V40GuardianLedgerHalted(
                "V40_GUARDIAN_RESTRICTED_DB_READ_FAILED"
            ) from None
