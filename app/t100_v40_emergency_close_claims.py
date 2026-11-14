"""Postgres immutable V40 emergency-close claim before ONE exchange POST.

Requires migration 040, exact restricted executor runtime role, serializable
transaction, shared account advisory lock, and linked OPEN checkpoint /
ACKed entry order / PROTECTED V40 slot. No fabricated position ownership.

No SQL update can 'reset' or retry a close claim; even a failed/ambiguous
network send consumes its position_id and entry OID permanently.
"""
from __future__ import annotations

from decimal import Decimal as D
from typing import Any

from app.t100_live_db_inspect import EXPECTED_DATABASE
from app.t100_live_role_preflight import TARGET_ROLE
from app.t100_live_four_slot_reservations import ACCOUNT_LOCK_SQL
from app.t100_live_execution import deterministic_t100_entry_oid
from app.t100_v40_emergency_close import (
    V40EmergencyCloseTarget,V40EmergencyCloseHalted,
)

_READ_LINKED = """
SELECT c.position_id, c.symbol, c.external_oid,
       c.exchange_order_id, c.contracts, c.stop_order_id,
       c.checkpoint_version, c.lifecycle_state,
       i.symbol AS intent_symbol, i.external_oid AS intent_oid,
       i.state AS intent_state, i.exchange_order_id AS intent_order_id,
       i.volume_contracts AS intent_contracts,
       i.notional_usdt AS intent_notional,
       i.signal_id AS intent_signal_id,
       s.state AS slot_state, s.signal_id AS slot_signal_id,
       s.external_oid AS slot_oid
FROM public.t100_live_position_checkpoints AS c
JOIN public.t100_live_order_intents AS i
  ON i.external_oid = c.external_oid
JOIN public.t100_live_v40_slot_reservations AS s
  ON s.external_oid = c.external_oid
WHERE c.position_id=$1
FOR UPDATE OF c,i,s
"""

_PENDING = """
SELECT
 (SELECT COUNT(*)::int FROM public.t100_live_order_intents
  WHERE state IN ('PREPARED','SUBMITTING','UNKNOWN','REVIEW')) +
 (SELECT COUNT(*)::int FROM public.t100_live_stop_amendments
  WHERE state <> 'VERIFIED') AS pending
"""

_INSERT = """
INSERT INTO public.t100_live_v40_emergency_close_claims
 (position_id, entry_external_oid,close_external_oid,
  original_exchange_order_id,symbol,contracts,
  original_notional_usdt,checkpoint_version,reason)
VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9)
ON CONFLICT DO NOTHING
RETURNING position_id
"""

_PRIVILEGES = """
SELECT
    has_table_privilege(
      current_user,'public.t100_live_v40_emergency_close_claims',
      'SELECT') AS select_ok,
    has_table_privilege(
      current_user,'public.t100_live_v40_emergency_close_claims',
      'INSERT') AS insert_ok,
    has_table_privilege(
      current_user,'public.t100_live_v40_emergency_close_claims',
      'UPDATE,DELETE,TRUNCATE,REFERENCES,TRIGGER') AS mutation_ok
"""


class V40PostgresEmergencyCloseClaims:
    def __init__(self,pool:Any):
        if pool is None:
            raise V40EmergencyCloseHalted(
                "V40_EMERGENCY_CLOSE_RUNTIME_POOL_REQUIRED"
            )
        self.pool=pool

    async def reserve_one(self,target:V40EmergencyCloseTarget)->bool:
        if type(target) is not V40EmergencyCloseTarget:
            raise V40EmergencyCloseHalted(
                "V40_EMERGENCY_CLOSE_TARGET_TYPE_INVALID"
            )
        try:
            async with self.pool.acquire() as conn:
                async with conn.transaction(isolation="serializable"):
                    if (
                        await conn.fetchval("SELECT current_database()")
                            !=EXPECTED_DATABASE
                        or await conn.fetchval("SELECT current_user")
                            !=TARGET_ROLE
                        or await conn.fetchval(
                            "SELECT current_setting('transaction_isolation')"
                        )!="serializable"
                        or await conn.fetchval(
                            "SELECT current_setting('transaction_read_only')"
                        )!="off"
                    ):
                        raise V40EmergencyCloseHalted(
                            "V40_EMERGENCY_CLOSE_DB_ROLE_OR_TX_INVALID"
                        )
                    priv=await conn.fetchrow(_PRIVILEGES)
                    if (
                        priv is None
                        or priv["select_ok"] is not True
                        or priv["insert_ok"] is not True
                        or priv["mutation_ok"] is not False
                    ):
                        raise V40EmergencyCloseHalted(
                            "V40_EMERGENCY_CLOSE_IMMUTABLE_TABLE_PRIVILEGES_INVALID"
                        )
                    await conn.fetchval(ACCOUNT_LOCK_SQL)
                    linked=await conn.fetchrow(
                        _READ_LINKED,int(target.position_id),
                    )
                    if (
                        linked is None
                        or str(linked["position_id"])!=target.position_id
                        or linked["symbol"]!=target.symbol
                        or linked["external_oid"]!=target.entry_external_oid
                        or str(linked["exchange_order_id"])
                             !=target.original_order_id
                        or D(str(linked["contracts"]))
                             !=target.quantity_contracts
                        or str(linked["stop_order_id"])!=target.stop_order_id
                        or linked["checkpoint_version"]
                             !=target.checkpoint_version
                        or linked["lifecycle_state"]!="OPEN"
                        or linked["intent_symbol"]!=target.symbol
                        or linked["intent_oid"]!=target.entry_external_oid
                        or str(linked["intent_order_id"])
                             !=target.original_order_id
                        or D(str(linked["intent_contracts"]))
                             !=target.quantity_contracts
                        or D(str(linked["intent_notional"]))
                             !=target.original_notional_usdt
                        or linked["intent_state"]!="ACK"
                        or linked["slot_state"]!="PROTECTED"
                        or linked["slot_oid"]!=target.entry_external_oid
                        or type(linked["intent_signal_id"]) is not int
                        or linked["intent_signal_id"]<=0
                        or linked["slot_signal_id"]!=linked["intent_signal_id"]
                        or deterministic_t100_entry_oid(
                            linked["intent_signal_id"],target.symbol
                        )!=target.entry_external_oid
                    ):
                        raise V40EmergencyCloseHalted(
                            "V40_EMERGENCY_CLOSE_DB_ORIGINAL_OWNER_DRIFT"
                        )
                    pending=await conn.fetchval(_PENDING)
                    if type(pending) is not int or pending!=0:
                        raise V40EmergencyCloseHalted(
                            "V40_EMERGENCY_CLOSE_UNKNOWN_ENTRY_OR_STOP_PENDING"
                        )
                    result=await conn.fetchval(
                        _INSERT,int(target.position_id),
                        target.entry_external_oid,target.close_external_oid,
                        target.original_order_id,target.symbol,
                        target.quantity_contracts,target.original_notional_usdt,
                        target.checkpoint_version,target.cause,
                    )
                    return result is not None and str(result)==target.position_id
        except V40EmergencyCloseHalted:
            raise
        except Exception:
            # Commit response may fail AFTER the SQL INSERT committed.
            # This method never sends any MEXC POST: caller freezes.
            raise V40EmergencyCloseHalted(
                "V40_EMERGENCY_CLOSE_DB_CLAIM_UNCERTAIN_NO_POST"
            ) from None


    async def read_prior_claim(self)->V40EmergencyCloseTarget|None:
        """Signed-GET reconciliation identity ONLY. Never another close POST."""
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
                    rows=await conn.fetch(
                        """SELECT position_id,entry_external_oid,
                                  close_external_oid,
                                  original_exchange_order_id,symbol,
                                  contracts,original_notional_usdt,
                                  checkpoint_version,reason,claim_status
                           FROM public.t100_live_v40_emergency_close_claims
                           ORDER BY claimed_at,position_id
                           LIMIT 2"""
                    )
                    if type(rows) not in (list,tuple) or len(rows)>1:
                        raise ValueError
                    if not rows:
                        return None
                    row=rows[0]
                    if (
                        row["reason"]!="NATIVE_STOP_MISSING_DOUBLE_GET"
                        or row["claim_status"]!="POST_MAY_HAVE_OCCURRED"
                        or type(row["checkpoint_version"]) is not int
                        or row["checkpoint_version"]<=0
                        or D(str(row["contracts"]))<=0
                        or not D(0)<D(str(row["original_notional_usdt"]))
                            <=D("12.50")
                    ):
                        raise ValueError
                    # Existing claim remains immutable after a close.
                    # Stop order ID is read from the original checkpoint
                    # below; it cannot be guessed from a missing GET.
                    cp=await conn.fetchrow(
                        """SELECT stop_order_id FROM
                           public.t100_live_position_checkpoints
                           WHERE position_id=$1 AND external_oid=$2
                             AND exchange_order_id=$3""",
                        row["position_id"],row["entry_external_oid"],
                        row["original_exchange_order_id"],
                    )
                    if cp is None:
                        raise ValueError
                    return V40EmergencyCloseTarget(
                        symbol=row["symbol"],
                        position_id=str(row["position_id"]),
                        entry_external_oid=row["entry_external_oid"],
                        original_order_id=row["original_exchange_order_id"],
                        stop_order_id=str(cp["stop_order_id"]),
                        checkpoint_version=row["checkpoint_version"],
                        quantity_contracts=D(str(row["contracts"])),
                        original_notional_usdt=D(
                            str(row["original_notional_usdt"])
                        ),
                        close_external_oid=row["close_external_oid"],
                        # Dummy positive value is NEVER used to POST:
                        # this method returns reconciliation identity only.
                        reference_ask=D("1"),
                    )
        except Exception:
            raise V40EmergencyCloseHalted(
                "V40_EMERGENCY_CLOSE_PRIOR_CLAIM_READ_FAILED_NO_POST"
            ) from None


async def verify_runtime_first_canary_close_claim_schema(pool:Any)->bool:
    """Live pilot MUST see migrated 040 table and minimal runtime grants.

    This READ ONLY check is independent of the scanner owner's earlier
    migration log. Prior close claims mean this is no longer a first canary
    and must NEVER be rearmed by a new process.
    """
    if pool is None:
        return False
    try:
        async with pool.acquire() as conn:
            async with conn.transaction(
                isolation="repeatable_read",readonly=True,
            ):
                role=await conn.fetchval("SELECT current_user")
                db=await conn.fetchval("SELECT current_database()")
                read_only=await conn.fetchval(
                    "SELECT current_setting('transaction_read_only')"
                )
                if (
                    role!=TARGET_ROLE or db!=EXPECTED_DATABASE
                    or read_only!="on"
                ):
                    return False
                table=await conn.fetchval(
                    "SELECT to_regclass('public.t100_live_v40_emergency_close_claims') IS NOT NULL"
                )
                if table is not True:
                    return False
                privileges=await conn.fetchrow(
                    """SELECT
                         has_table_privilege(current_user,
                           'public.t100_live_v40_emergency_close_claims',
                           'SELECT') AS can_select,
                         has_table_privilege(current_user,
                           'public.t100_live_v40_emergency_close_claims',
                           'INSERT') AS can_insert,
                         has_table_privilege(current_user,
                           'public.t100_live_v40_emergency_close_claims',
                           'UPDATE,DELETE,TRUNCATE,TRIGGER') AS can_mutate,
                         EXISTS (
                           SELECT 1 FROM pg_trigger t
                           JOIN pg_class c ON c.oid=t.tgrelid
                           JOIN pg_namespace n ON n.oid=c.relnamespace
                           WHERE n.nspname='public'
                             AND c.relname='t100_live_v40_emergency_close_claims'
                             AND t.tgname='t100_v40_emergency_claim_immutable'
                             AND t.tgenabled='O' AND NOT t.tgisinternal
                         ) AS immutable_trigger"""
                )
                if privileges is None or any((
                    privileges["can_select"] is not True,
                    privileges["can_insert"] is not True,
                    privileges["can_mutate"] is not False,
                    privileges["immutable_trigger"] is not True,
                )):
                    return False
                number=await conn.fetchval(
                    """SELECT count(*)::int
                       FROM public.t100_live_v40_emergency_close_claims"""
                )
                return type(number) is int and number==0
    except Exception:
        return False
