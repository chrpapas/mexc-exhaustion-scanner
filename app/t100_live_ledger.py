"""Durable PostgreSQL order intents, separate from the T100 paper ledger."""
from __future__ import annotations
from app.t100_live_execution import EntryIntent

class PostgresIntentLedger:
    def __init__(self, pool):
        self.pool = pool

    async def insert_prepared(self, intent: EntryIntent) -> bool:
        row = await self.pool.fetchval(
            """INSERT INTO t100_live_order_intents
               (signal_id,symbol,external_oid,reference_price,notional_usdt,volume_contracts,stop_price)
               VALUES($1,$2,$3,$4,$5,$6,$7)
               ON CONFLICT DO NOTHING RETURNING id""",
            intent.signal_id, intent.symbol, intent.external_oid,
            intent.reference_price, intent.notional_usdt,
            intent.volume_contracts, intent.stop_price,
        )
        return row is not None

    async def transition(self, oid: str, expected: str, next_state: str,
                         *, order_id: str | None = None, reason: str | None = None) -> bool:
        row = await self.pool.fetchval(
            """UPDATE t100_live_order_intents
               SET state=$3,exchange_order_id=COALESCE($4,exchange_order_id),
                   failure_reason=$5,updated_at=now()
               WHERE external_oid=$1 AND state=$2 RETURNING id""",
            oid, expected, next_state, order_id, reason,
        )
        return row is not None

    async def state(self, oid: str) -> str:
        state = await self.pool.fetchval(
            "SELECT state FROM t100_live_order_intents WHERE external_oid=$1", oid)
        if state is None:
            raise RuntimeError("UNKNOWN_EXTERNAL_OID")
        return str(state)
