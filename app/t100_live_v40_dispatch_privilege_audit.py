"""Restricted V40 one-shot dispatch-claim permission/immutability audit.

Read-only inspection ONLY. Checks effective rights, ownership and whether
the protective trigger is enabled. Does NOT grant roles or edit tables,
enable exchange POSTs, or authorize real trading. Migration 032 is not
approved for production deployment.
"""
from __future__ import annotations

from typing import Any

from app.t100_live_db_inspect import EXPECTED_DATABASE
from app.t100_live_role_preflight import TARGET_ROLE

CLAIM_PRIVILEGES_SQL = """
SELECT
    pg_get_userbyid(c.relowner) AS table_owner,
    has_table_privilege(current_user,c.oid,'SELECT') AS can_select,
    has_table_privilege(current_user,c.oid,'INSERT') AS can_insert,
    has_table_privilege(current_user,c.oid,'UPDATE') AS can_update,
    has_table_privilege(current_user,c.oid,'DELETE') AS can_delete,
    has_table_privilege(current_user,c.oid,'TRUNCATE') AS can_truncate,
    has_table_privilege(current_user,c.oid,'TRIGGER') AS can_trigger,
    has_schema_privilege(current_user,'public','CREATE')
        AS can_create_schema_objects,
    has_database_privilege(current_user,current_database(),'CREATE')
        AS can_create_database_objects,
    EXISTS (
        SELECT 1 FROM pg_catalog.pg_trigger AS t
        WHERE t.tgrelid=c.oid
          AND t.tgname='t100_v40_dispatch_claim_immutable'
          AND t.tgenabled IN ('O','A')
          AND t.tgisinternal IS FALSE
    ) AS immutable_trigger_enabled
FROM pg_catalog.pg_class AS c
JOIN pg_catalog.pg_namespace AS n ON n.oid=c.relnamespace
WHERE n.nspname='public'
  AND c.relname='t100_live_v40_dispatch_claims'
  AND c.relkind='r'
"""


async def assess_v40_dispatch_claim_privileges(conn: Any) -> dict[str, Any]:
    """Requires the caller's verified restricted read-only transaction."""
    result: dict[str, Any] = {
        "read_only": True,
        "runtime_role_verified": False,
        "dispatch_claim_privileges_verified": False,
        "immutable_trigger_verified": False,
        "runtime_ddl_rights_absent": False,
        "live_authorization": False,
        "entry_allowed": False,
        "mexc_orders_sent": False,
    }
    if conn is None:
        result["status"] = "V40_DISPATCH_PRIVILEGE_CONNECTION_MISSING"
        return result
    try:
        readonly = await conn.fetchval(
            "SELECT current_setting('transaction_read_only')"
        )
        isolation = await conn.fetchval(
            "SELECT current_setting('transaction_isolation')"
        )
        db = await conn.fetchval("SELECT current_database()")
        role = await conn.fetchval("SELECT current_user")
        if (
            readonly != "on"
            or isolation != "repeatable read"
            or db != EXPECTED_DATABASE
            or role != TARGET_ROLE
        ):
            result["status"] = "V40_DISPATCH_RESTRICTED_CONTEXT_UNVERIFIED"
            return result
        result["runtime_role_verified"] = True
        row = await conn.fetchrow(CLAIM_PRIVILEGES_SQL)
        if row is None:
            result["status"] = "V40_DISPATCH_CLAIM_TABLE_NOT_OBSERVED"
            return result
        expected_boolean_fields = (
            "can_select", "can_insert", "can_update", "can_delete",
            "can_truncate", "can_trigger", "can_create_schema_objects",
            "can_create_database_objects", "immutable_trigger_enabled",
        )
        if any(type(row[field]) is not bool for field in expected_boolean_fields):
            result["status"] = "V40_DISPATCH_PRIVILEGE_SHAPE_INVALID"
            return result
        owner = row["table_owner"]
        if type(owner) is not str or not owner:
            result["status"] = "V40_DISPATCH_CLAIM_OWNER_UNVERIFIED"
            return result
        result["immutable_trigger_verified"] = (
            row["immutable_trigger_enabled"] is True
        )
        result["runtime_ddl_rights_absent"] = (
            owner != TARGET_ROLE
            and row["can_create_schema_objects"] is False
            and row["can_create_database_objects"] is False
            and row["can_trigger"] is False
        )
        result["dispatch_claim_privileges_verified"] = (
            row["can_select"] is True
            and row["can_insert"] is True
            and row["can_update"] is False
            and row["can_delete"] is False
            and row["can_truncate"] is False
            and result["runtime_ddl_rights_absent"] is True
            and result["immutable_trigger_verified"] is True
        )
        result["status"] = (
            "V40_DISPATCH_CLAIM_PRIVILEGES_VERIFIED_NONAUTHORIZING"
            if result["dispatch_claim_privileges_verified"]
            else "V40_DISPATCH_CLAIM_PRIVILEGES_BLOCKED"
        )
        return result
    except Exception:
        result["status"] = "V40_DISPATCH_PRIVILEGE_AUDIT_READ_FAILED"
        return result
