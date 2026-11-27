"""Reconcile-only V40 emergency close finalization; NEVER place MEXC orders.

Only after exact completed close-order+fills and two signed flat bookends:
persist immutable CLOSE_SHORT fills, CAS the original OPEN -> CLOSING ->
CLOSED checkpoint and intent, and release the protected first-canary slot.
Repeat invocation can ONLY finish a previously verified close. The immutable
emergency close claim is NEVER deleted or reset.

Every SQL unknown leaves new entries disabled and requires re-observation.
There is no exchange POST in this module.
"""
from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal as D,InvalidOperation
from typing import Any

from app.t100_live_db_inspect import EXPECTED_DATABASE
from app.t100_live_role_preflight import TARGET_ROLE
from app.t100_live_four_slot_reservations import ACCOUNT_LOCK_SQL
from app.t100_live_checkpoint_journal import (
    PostgresCheckpointJournal,build_lifecycle_checkpoint,
    CLOSING_LATCHED,CLOSE_VERIFIED,
)
from app.t100_live_fill_ledger import PostgresVerifiedFillLedger
from app.t100_live_durable_recovery import PositionCheckpoint
from app.t100_v40_emergency_close import V40EmergencyCloseTarget
from app.t100_v40_emergency_postflight import V40EmergencyClosePostflight


class V40EmergencyCompletionHalted(RuntimeError):
    pass


_CLAIM = """
SELECT position_id,entry_external_oid,close_external_oid,
       original_exchange_order_id,symbol,contracts,
       original_notional_usdt,checkpoint_version,reason,claim_status
FROM public.t100_live_v40_emergency_close_claims
WHERE position_id=$1
"""

_CHECKPOINT = """
SELECT position_id,symbol,external_oid,exchange_order_id,contracts,
       entry_price,stop_order_id,stop_price,best_profit_pct,armed,
       lifecycle_state,checkpoint_version
FROM public.t100_live_position_checkpoints
WHERE position_id=$1
"""

_RELEASE = """
UPDATE public.t100_live_v40_slot_reservations AS s
SET state='RELEASED',updated_at=clock_timestamp()
WHERE s.external_oid=$1 AND s.state='PROTECTED'
  AND EXISTS (
    SELECT 1 FROM public.t100_live_position_checkpoints AS p
    JOIN public.t100_live_order_intents AS i
      ON i.external_oid=p.external_oid
    JOIN public.t100_live_v40_emergency_close_claims AS c
      ON c.entry_external_oid=p.external_oid
    WHERE p.position_id=$2 AND p.external_oid=$1
      AND p.lifecycle_state='CLOSED' AND i.state='CLOSED'
      AND c.position_id=p.position_id AND c.close_external_oid=$3
      AND s.signal_id=i.signal_id
  )
RETURNING s.signal_id
"""

_RELEASED = """
SELECT s.state
FROM public.t100_live_v40_slot_reservations AS s
JOIN public.t100_live_position_checkpoints AS p
  ON p.external_oid=s.external_oid
JOIN public.t100_live_order_intents AS i
  ON i.external_oid=s.external_oid
WHERE p.position_id=$2 AND s.external_oid=$1
  AND p.lifecycle_state='CLOSED' AND i.state='CLOSED'
"""


@dataclass(frozen=True)
class V40EmergencyCompletionResult:
    status:str
    immutable_fill_ledger_verified:bool=False
    original_checkpoint_closed:bool=False
    original_intent_closed:bool=False
    first_slot_released:bool=False
    exchange_posts:int=0
    further_close_post_allowed:bool=False
    further_entries_allowed:bool=False
    live_authorization:bool=False


def _cp_from_sql(row:Any)->PositionCheckpoint:
    try:
        if row is None:
            raise ValueError
        values=dict(row)
        return PositionCheckpoint(
            symbol=str(values["symbol"]),
            external_oid=str(values["external_oid"]),
            exchange_order_id=str(values["exchange_order_id"]),
            position_id=str(values["position_id"]),
            contracts=D(str(values["contracts"])),
            entry_price=D(str(values["entry_price"])),
            stop_order_id=str(values["stop_order_id"]),
            stop_price=D(str(values["stop_price"])),
            best_profit_pct=D(str(values["best_profit_pct"])),
            armed=values["armed"],
            lifecycle_state=values["lifecycle_state"],
            checkpoint_version=values["checkpoint_version"],
        )
    except Exception:
        raise V40EmergencyCompletionHalted(
            "V40_EMERGENCY_COMPLETION_CHECKPOINT_SQL_INVALID"
        ) from None


class PostgresV40EmergencyCompletion:
    def __init__(self,pool:Any):
        if pool is None:
            raise V40EmergencyCompletionHalted(
                "V40_EMERGENCY_COMPLETION_RUNTIME_POOL_MISSING"
            )
        self.pool=pool

    async def _read(self,target:V40EmergencyCloseTarget)->PositionCheckpoint:
        try:
            async with self.pool.acquire() as conn:
                async with conn.transaction(
                    isolation="repeatable_read",readonly=True,
                ):
                    if (
                        await conn.fetchval("SELECT current_database()")
                             !=EXPECTED_DATABASE
                        or await conn.fetchval("SELECT current_user")
                             !=TARGET_ROLE
                        or await conn.fetchval(
                            "SELECT current_setting('transaction_read_only')"
                        )!="on"
                    ):
                        raise ValueError
                    claim=await conn.fetchrow(
                        _CLAIM,int(target.position_id),
                    )
                    if (
                        claim is None
                        or str(claim["position_id"])!=target.position_id
                        or claim["entry_external_oid"]!=target.entry_external_oid
                        or claim["close_external_oid"]!=target.close_external_oid
                        or claim["original_exchange_order_id"]
                            !=target.original_order_id
                        or claim["symbol"]!=target.symbol
                        or D(str(claim["contracts"]))!=target.quantity_contracts
                        or D(str(claim["original_notional_usdt"]))
                            !=target.original_notional_usdt
                        or claim["checkpoint_version"]!=target.checkpoint_version
                        or claim["reason"]!=target.cause
                        or claim["claim_status"]!="POST_MAY_HAVE_OCCURRED"
                    ):
                        raise ValueError
                    row=await conn.fetchrow(
                        _CHECKPOINT,int(target.position_id),
                    )
                    cp=_cp_from_sql(row)
                    if (
                        cp.symbol!=target.symbol
                        or cp.position_id!=target.position_id
                        or cp.external_oid!=target.entry_external_oid
                        or cp.exchange_order_id!=target.original_order_id
                        or cp.stop_order_id!=target.stop_order_id
                        or cp.contracts!=target.quantity_contracts
                        or (
                            cp.checkpoint_version,cp.lifecycle_state
                        ) not in (
                            (target.checkpoint_version,"OPEN"),
                            (target.checkpoint_version+1,"CLOSING"),
                            (target.checkpoint_version+2,"CLOSED"),
                        )
                    ):
                        raise ValueError
                    return cp
        except Exception:
            raise V40EmergencyCompletionHalted(
                "V40_EMERGENCY_COMPLETION_EXACT_CLAIM_OR_CHECKPOINT_MISSING"
            ) from None

    async def _release(self,target:V40EmergencyCloseTarget)->bool:
        try:
            async with self.pool.acquire() as conn:
                async with conn.transaction(isolation="serializable"):
                    if (
                        await conn.fetchval("SELECT current_database()")
                            !=EXPECTED_DATABASE
                        or await conn.fetchval("SELECT current_user")
                            !=TARGET_ROLE
                    ):
                        raise ValueError
                    await conn.fetchval(ACCOUNT_LOCK_SQL)
                    released=await conn.fetchval(
                        _RELEASE,target.entry_external_oid,
                        int(target.position_id),target.close_external_oid,
                    )
                    if released is not None:
                        return type(released) is int and released>0
                    existing=await conn.fetchrow(
                        _RELEASED,target.entry_external_oid,
                        int(target.position_id),
                    )
                    return existing is not None and existing["state"]=="RELEASED"
        except Exception:
            raise V40EmergencyCompletionHalted(
                "V40_EMERGENCY_COMPLETION_SLOT_RELEASE_UNVERIFIED"
            ) from None

    async def finalize_verified(
        self,*,target:V40EmergencyCloseTarget,
        postflight:V40EmergencyClosePostflight,
    )->V40EmergencyCompletionResult:
        if (
            type(target) is not V40EmergencyCloseTarget
            or type(postflight) is not V40EmergencyClosePostflight
            or postflight.status!=
                "V40_EMERGENCY_CLOSE_ORDER_AND_FLAT_ACCOUNT_DOUBLE_GET_VERIFIED"
            or postflight.exact_position_exit_verified is not True
            or postflight.full_account_flat_double_get_verified is not True
            or postflight.exact_close_fills_verified is not True
            or postflight.exact_close_fill_set is None
            or postflight.exact_close_order_id!=
                postflight.exact_close_fill_set.order_id
            or postflight.exact_close_fill_set.position_id!=target.position_id
            or postflight.exact_close_fill_set.symbol!=target.symbol
            or postflight.exact_close_fill_set.side!="CLOSE_SHORT"
            or postflight.exact_close_fill_set.deal_volume
                 !=target.quantity_contracts
        ):
            raise V40EmergencyCompletionHalted(
                "V40_EMERGENCY_COMPLETION_SIGNED_FLAT_AND_FILLS_REQUIRED"
            )
        current=await self._read(target)
        try:
            recorded=await PostgresVerifiedFillLedger(self.pool).persist(
                postflight.exact_close_fill_set
            )
            if recorded not in ("PERSISTED","ALREADY_VERIFIED"):
                raise ValueError
        except Exception:
            raise V40EmergencyCompletionHalted(
                "V40_EMERGENCY_COMPLETION_IMMUTABLE_FILLS_UNVERIFIED"
            ) from None
        journal=PostgresCheckpointJournal(self.pool)
        try:
            if current.lifecycle_state=="OPEN":
                closing=build_lifecycle_checkpoint(
                    current,target="CLOSING",
                )
                await journal.advance(
                    current,closing,reason=CLOSING_LATCHED,
                )
                current=closing
            if current.lifecycle_state=="CLOSING":
                closed=build_lifecycle_checkpoint(
                    current,target="CLOSED",position_absent=True,
                    stop_absent=True,close_provenance_verified=True,
                )
                await journal.advance(
                    current,closed,reason=CLOSE_VERIFIED,
                )
                current=closed
            if current.lifecycle_state!="CLOSED":
                raise ValueError
            if not await self._release(target):
                raise ValueError
        except Exception:
            # A failed DB acknowledgement could have committed. The durable
            # close claim remains; restart only GETs and retries SQL CAS.
            raise V40EmergencyCompletionHalted(
                "V40_EMERGENCY_COMPLETION_DB_COMMIT_UNKNOWN_RECONCILE_ONLY"
            ) from None
        return V40EmergencyCompletionResult(
            "V40_EMERGENCY_CLOSE_CHECKPOINT_INTENT_AND_SLOT_CLOSED_VERIFIED",
            immutable_fill_ledger_verified=True,
            original_checkpoint_closed=True,original_intent_closed=True,
            first_slot_released=True,
        )
