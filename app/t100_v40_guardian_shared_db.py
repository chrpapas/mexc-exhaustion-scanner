"""Opt-in single-existing-V40-runtime-DSN guardian compatibility.

This intentionally trades a degree of DB credential isolation for simpler
first-pilot setup: the guardian reuses the restricted executor runtime login
for both READ-ONLY journal snapshots and sticky incident/outbox writes.
DO NOT use the database owner/mexc_scanner, scanner signal reader, or a
trading API key for this mode. No grant, schema change or exchange POST occurs
inside this module. The operator must explicitly grant only the incident
tables and their sequence to the existing runtime role first.

An independent deployed observer is NOT proven merely by successful SQL.
The active native MARKET SL checks and the first real protected position
remain separate requirements; this mode cannot issue a live trading permit.
"""
from __future__ import annotations

from typing import Any

from app.t100_live_db_inspect import EXPECTED_DATABASE
from app.t100_live_role_preflight import TARGET_ROLE


class V40SharedGuardianDatabaseHalted(RuntimeError):
    pass


# The existing executor's immutable journal-specific INSERT/CAS grants are
# intentionally not revoked here; reuse mode is weaker than two DB roles.
# Require both incident-outbox write rights and NO superuser/CREATE/DELETE.
_REQUIRED_TABLES=(
    "public.t100_guardian_incident_state",
    "public.t100_guardian_incident_outbox",
)

async def verify_existing_runtime_for_guardian(conn:Any)->None:
    """Check exact runtime role and incident permissions without changing DB."""
    try:
        current=await conn.fetchrow(
            """SELECT current_user AS role,
                      current_database() AS db,
                      current_setting('transaction_read_only') AS read_only,
                      has_schema_privilege(current_user,'public','CREATE')
                        AS schema_create,
                      has_database_privilege(current_user,current_database(),
                        'CREATE') AS database_create,
                      (SELECT rolsuper OR rolcreaterole OR rolcreatedb
                              OR rolbypassrls FROM pg_catalog.pg_roles
                        WHERE rolname=current_user) AS admin_role"""
        )
        if (
            current is None
            or current["role"]!=TARGET_ROLE
            or current["db"]!=EXPECTED_DATABASE
            or current["read_only"]!="off"
            or current["schema_create"] is not False
            or current["database_create"] is not False
            or current["admin_role"] is not False
        ):
            raise V40SharedGuardianDatabaseHalted(
                "V40_SHARED_GUARDIAN_NOT_RESTRICTED_EXECUTOR_ROLE"
            )
        grants=await conn.fetch(
            """SELECT table_name,
                 has_table_privilege(current_user,table_name,
                   'SELECT') AS can_select,
                 has_table_privilege(current_user,table_name,
                   'INSERT') AS can_insert,
                 has_table_privilege(current_user,table_name,
                   'UPDATE') AS can_update,
                 has_table_privilege(current_user,table_name,
                   'DELETE,TRUNCATE') AS can_delete_or_truncate
               FROM unnest($1::text[]) AS table_name""",
            list(_REQUIRED_TABLES),
        )
        if (
            type(grants) not in (list,tuple)
            or len(grants)!=2
            or any(
                row["table_name"] not in _REQUIRED_TABLES
                or row["can_select"] is not True
                or row["can_insert"] is not True
                or row["can_update"] is not True
                or row["can_delete_or_truncate"] is not False
                for row in grants
            )
        ):
            raise V40SharedGuardianDatabaseHalted(
                "V40_SHARED_GUARDIAN_INCIDENT_GRANTS_NOT_APPLIED"
            )
    except V40SharedGuardianDatabaseHalted:
        raise
    except Exception:
        raise V40SharedGuardianDatabaseHalted(
            "V40_SHARED_GUARDIAN_DATABASE_PERMISSION_AUDIT_FAILED"
        ) from None


async def shared_guardian_connection(dsn:str,unused_ca:str)->Any:
    """Render PostgreSQL TLS required, no new CA file or new DB login."""
    if unused_ca!="USE_EXISTING_V40_RUNTIME_TLS":
        raise V40SharedGuardianDatabaseHalted(
            "V40_SHARED_GUARDIAN_TLS_MODE_INVALID"
        )
    import asyncpg
    try:
        return await asyncpg.connect(
            dsn,ssl="require",timeout=8,command_timeout=15,
        )
    except Exception:
        raise V40SharedGuardianDatabaseHalted(
            "V40_SHARED_GUARDIAN_DATABASE_CONNECT_FAILED"
        ) from None
