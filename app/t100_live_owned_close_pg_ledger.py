"""PostgreSQL adapter for migration 033's T100-owned close intent ledger.

Never issues an exchange request. No schema creation, login roles or
auto-migrations. The injected connection must be a narrowly scoped role
with SELECT/INSERT/UPDATE on ONLY the reviewed T100 close-intent table.
"""
from __future__ import annotations

from typing import Any

from app.t100_live_owned_emergency_guardian import (
    OwnedGuardianHalted, OwnedShort, _validate_owned, close_intent_oid,
)


class PostgresOwnedCloseIntentLedger:
    def __init__(self, connection: Any):
        self.connection = connection

    async def prepare_if_absent(self, external_oid: str,
                                owned: OwnedShort) -> bool:
        _validate_owned(owned, owned.scope_sha256)
        if external_oid != close_intent_oid(owned):
            raise OwnedGuardianHalted("EMERGENCY_CLOSE_OID_IDENTITY_MISMATCH")
        # PostgreSQL's unique scope+position and primary key ensure that a
        # new process cannot mint a second close request for the same trade.
        try:
            row = await self.connection.fetchval(
                """INSERT INTO public.t100_owned_emergency_close_intents
                   (external_oid, scope_sha256, symbol, position_id,
                    entry_external_oid, entry_order_id, volume_contracts,
                    checkpoint_version, state)
                   VALUES ($1,$2,$3,$4,$5,$6,$7,$8,'PREPARED')
                   ON CONFLICT DO NOTHING RETURNING external_oid""",
                external_oid, owned.scope_sha256, owned.symbol,
                owned.position_id, owned.entry_external_oid,
                owned.entry_order_id, owned.contracts,
                owned.checkpoint_version,
            )
        except Exception:
            raise OwnedGuardianHalted("EMERGENCY_CLOSE_LEDGER_INSERT_FAILED") from None
        return row == external_oid

    async def transition(self, external_oid: str, expected: str,
                         target: str) -> bool:
        if not isinstance(external_oid, str) or not (
            external_oid.startswith("t100gc_") and len(external_oid) == 29
        ):
            raise OwnedGuardianHalted("EMERGENCY_CLOSE_EXTERNAL_ID_INVALID")
        allowed = {("PREPARED", "SUBMITTING"),
                   ("SUBMITTING", "UNKNOWN"),
                   ("SUBMITTING", "VERIFIED"),
                   ("UNKNOWN", "VERIFIED")}
        if (expected, target) not in allowed:
            raise OwnedGuardianHalted("EMERGENCY_CLOSE_ILLEGAL_STATE_CHANGE")
        try:
            row = await self.connection.fetchval(
                """UPDATE public.t100_owned_emergency_close_intents
                   SET state=$3, updated_at=now()
                   WHERE external_oid=$1 AND state=$2
                   RETURNING external_oid""",
                external_oid, expected, target,
            )
        except Exception:
            raise OwnedGuardianHalted("EMERGENCY_CLOSE_LEDGER_CAS_FAILED") from None
        return row == external_oid
