"""Read-only, target-role ACL audit for the existing T100 live database.

Runs only catalog SELECTs in a READ ONLY transaction. It does not change
roles/grants, create objects, apply migrations, access MEXC or authorize live.
Use the existing Render owner's External Database URL at the hidden prompt.
"""
from __future__ import annotations

import argparse
import asyncio
import getpass
import json
import os
import ssl
from typing import Any

from app.t100_live_db_inspect import (
    ALLOWED_STRATEGY_UPDATES,
    EXPECTED_DATABASE,
    IMMUTABLE_STRATEGY_COLUMNS,
    LEASE_TABLE,
    STRATEGY_SEQUENCE,
    STRATEGY_TABLE_COLUMNS,
)
from app.t100_live_schema_gate import (
    LIVE_TABLE_COLUMNS,
    MUTABLE_TABLES,
    REQUIRED_SEQUENCES,
)

ROLE = "t100_live_executor_runtime"
TABLES = tuple(sorted(set(LIVE_TABLE_COLUMNS) | set(STRATEGY_TABLE_COLUMNS) | {LEASE_TABLE}))
SEQUENCES = tuple(sorted(set(REQUIRED_SEQUENCES) | {STRATEGY_SEQUENCE}))
STRATEGY_COLUMNS = tuple(sorted(
    (table, column)
    for table in ALLOWED_STRATEGY_UPDATES
    for column in (
        ALLOWED_STRATEGY_UPDATES[table] | IMMUTABLE_STRATEGY_COLUMNS[table]
    )
))


async def inspect_target_role_acl(conn: Any) -> dict[str, Any]:
    result: dict[str, Any] = {
        "status": "TARGET_ROLE_ACL_UNVERIFIED",
        "target_role": ROLE,
        "read_only": True,
        "live_authorization": False,
        "orders_sent": 0,
        "database_mutations": 0,
    }
    try:
        if await conn.fetchval("SELECT current_setting('transaction_read_only')") != "on":
            result["status"] = "NOT_IN_READ_ONLY_TRANSACTION"
            return result
        if await conn.fetchval("SELECT current_database()") != EXPECTED_DATABASE:
            result["status"] = "WRONG_DATABASE_IDENTITY"
            return result

        attributes = await conn.fetchrow(
            """SELECT rolcanlogin,rolsuper,rolcreaterole,rolcreatedb,
                      rolreplication,rolbypassrls
               FROM pg_catalog.pg_roles WHERE rolname=$1""", ROLE)
        if attributes is None:
            result["status"] = "TARGET_ROLE_NOT_FOUND"
            return result
        role_privs = await conn.fetchrow(
            """SELECT pg_has_role(session_user,$1::name,'SET') AS owner_can_set_role,
                      has_schema_privilege($1::name,'public','CREATE')
                        AS can_create_in_public,
                      has_database_privilege($1::name,current_database(),'CREATE')
                        AS can_create_in_database""", ROLE)
        if role_privs is None:
            result["status"] = "TARGET_ROLE_PRIVILEGES_UNAVAILABLE"
            return result

        table_rows = await conn.fetch(
            """SELECT table_name,
                      has_table_privilege($1::name,
                        format('public.%I',table_name),'SELECT') AS can_select,
                      has_table_privilege($1::name,
                        format('public.%I',table_name),'INSERT') AS can_insert,
                      has_table_privilege($1::name,
                        format('public.%I',table_name),'UPDATE') AS can_update,
                      has_table_privilege($1::name,
                        format('public.%I',table_name),'DELETE') AS can_delete,
                      has_table_privilege($1::name,
                        format('public.%I',table_name),'TRUNCATE') AS can_truncate
               FROM unnest($2::text[]) AS t(table_name)""",
            ROLE, list(TABLES))
        sequence_rows = await conn.fetch(
            """SELECT sequence_name,
                      has_sequence_privilege($1::name,
                        format('public.%I',sequence_name),'USAGE') AS can_usage,
                      has_sequence_privilege($1::name,
                        format('public.%I',sequence_name),'SELECT') AS can_select,
                      has_sequence_privilege($1::name,
                        format('public.%I',sequence_name),'UPDATE') AS can_update
               FROM unnest($2::text[]) AS t(sequence_name)""",
            ROLE, list(SEQUENCES))
        column_rows = await conn.fetch(
            """SELECT table_name,column_name,
                      has_column_privilege($1::name,
                        format('public.%I',table_name),
                        column_name,'UPDATE') AS can_update
               FROM unnest($2::text[],$3::text[]) AS t(table_name,column_name)""",
            ROLE, [x[0] for x in STRATEGY_COLUMNS],
            [x[1] for x in STRATEGY_COLUMNS])
    except Exception as exc:
        result["status"] = "TARGET_ROLE_ACL_READ_FAILED"
        result["error_class"] = type(exc).__name__
        return result

    # Missing or duplicate evidence is never a pass.
    tables = {row["table_name"]: row for row in table_rows}
    sequences = {row["sequence_name"]: row for row in sequence_rows}
    columns = {(row["table_name"], row["column_name"]): row
               for row in column_rows}
    if (len(table_rows) != len(TABLES) or set(tables) != set(TABLES)
            or len(sequence_rows) != len(SEQUENCES)
            or set(sequences) != set(SEQUENCES)
            or len(column_rows) != len(STRATEGY_COLUMNS)
            or set(columns) != set(STRATEGY_COLUMNS)):
        result["status"] = "TARGET_ROLE_ACL_EVIDENCE_INCOMPLETE"
        return result

    issues: list[str] = []
    for key in ("rolsuper", "rolcreaterole", "rolcreatedb",
                "rolreplication", "rolbypassrls"):
        if attributes[key] is not False:
            issues.append("ROLE_UNSAFE_" + key)
    if attributes["rolcanlogin"] is not True:
        issues.append("ROLE_LOGIN_MISSING")
    for key in ("can_create_in_public", "can_create_in_database"):
        if role_privs[key] is not False:
            issues.append("ROLE_UNSAFE_" + key)
    for table in TABLES:
        expected = {
            "can_select": True,
            "can_insert": True,
            "can_update": table in MUTABLE_TABLES,
            "can_delete": False,
            "can_truncate": False,
        }
        for privilege, should_have in expected.items():
            if tables[table][privilege] is not should_have:
                issues.append("TABLE:" + table + ":" + privilege
                              + ":expected_" + str(should_have).lower())
    for seq in SEQUENCES:
        for privilege, should_have in (
            ("can_usage", True), ("can_select", True), ("can_update", False),
        ):
            if sequences[seq][privilege] is not should_have:
                issues.append("SEQUENCE:" + seq + ":" + privilege
                              + ":expected_" + str(should_have).lower())
    for table, column in STRATEGY_COLUMNS:
        should_have = column in ALLOWED_STRATEGY_UPDATES[table]
        if columns[(table, column)]["can_update"] is not should_have:
            issues.append("COLUMN:" + table + "." + column
                          + ":can_update:expected_" + str(should_have).lower())

    result.update({
        "status": ("TARGET_ROLE_ACL_MATCH_NONAUTHORIZING"
                   if not issues else "TARGET_ROLE_ACL_MISMATCH"),
        "role_can_be_set_by_current_session": role_privs["owner_can_set_role"] is True,
        "current_session_is_target_role": (
            await conn.fetchval("SELECT current_user") == ROLE
        ),
        "table_count": len(tables),
        "sequence_count": len(sequences),
        "strategy_column_privileges_checked": len(columns),
        "mismatch_count": len(issues),
        "mismatches": issues,
    })
    return result


async def _main() -> None:
    if os.getenv("T100_EXECUTION_MODE", "paper").strip().lower() != "paper":
        raise RuntimeError("PAPER_MODE_REQUIRED")
    dsn = getpass.getpass("Render PostgreSQL External URL (hidden): ")
    if not dsn.startswith(("postgresql://", "postgres://")):
        raise RuntimeError("DATABASE_URL_INVALID")
    import asyncpg

    conn = None
    try:
        conn = await asyncpg.connect(
            dsn=dsn, ssl=ssl.create_default_context(),
            timeout=12, command_timeout=20)
        async with conn.transaction(readonly=True):
            result = await inspect_target_role_acl(conn)
        print(json.dumps(result, indent=2, sort_keys=True))
    except Exception as exc:
        # Never log the DSN or raw connection/driver errors.
        print(json.dumps({
            "status": "READ_ONLY_PROBE_BLOCKED",
            "error_class": type(exc).__name__,
            "live_authorization": False,
        }, sort_keys=True))
    finally:
        if conn is not None:
            await conn.close()


def cli() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.parse_args()
    asyncio.run(_main())


if __name__ == "__main__":
    cli()
