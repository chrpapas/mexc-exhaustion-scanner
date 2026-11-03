"""Read-only prerequisites for a separate T100 live PostgreSQL login.

This is deliberately NOT role creation or a GRANT runner. It verifies whether
the current, trusted database owner can provision a dedicated restricted role
without replacing Render's managed default database credential.

From a trusted machine:
    T100_EXECUTION_MODE=paper python -m app.t100_live_role_preflight --prompt-dsn

No MEXC calls, no trading authorization, no SQL writes, no credential prints.
"""
from __future__ import annotations

import argparse
import asyncio
import getpass
import json
import os
from typing import Any

from app.t100_live_db_inspect import EXPECTED_DATABASE, LEASE_TABLE, STRATEGY_TABLE_COLUMNS
from app.t100_live_schema_gate import LIVE_TABLE_COLUMNS, REQUIRED_SEQUENCES
from app.t100_live_db_inspect import STRATEGY_SEQUENCE

TARGET_ROLE = "t100_live_executor_runtime"
LIVE_TABLES = frozenset(LIVE_TABLE_COLUMNS) | frozenset(STRATEGY_TABLE_COLUMNS) | {LEASE_TABLE}
LIVE_SEQUENCES = frozenset(REQUIRED_SEQUENCES) | {STRATEGY_SEQUENCE}


async def inspect_role_setup_preconditions(conn: Any) -> dict[str, Any]:
    result: dict[str, Any] = {
        "read_only": True,
        "database_name_verified": False,
        "role_creation_allowed": False,
        "live_table_owner_verified": False,
        "live_sequence_owner_verified": False,
        "runtime_role_absent": False,
        "live_authorization": False,
        "mexc_orders_sent": False,
        "expected_live_tables": len(LIVE_TABLES),
        "expected_live_sequences": len(LIVE_SEQUENCES),
        "runtime_role_name": TARGET_ROLE,
    }
    try:
        readonly = await conn.fetchval(
            "SELECT current_setting('transaction_read_only')"
        )
        if readonly != "on":
            result["status"] = "NOT_READ_ONLY"
            return result
        db = await conn.fetchval("SELECT current_database()")
        if db != EXPECTED_DATABASE:
            result["status"] = "WRONG_DATABASE"
            return result
        result["database_name_verified"] = True

        user = await conn.fetchrow(
            """SELECT current_user AS username,
                      r.rolcreaterole AS can_create_role,
                      r.rolsuper AS is_superuser
                 FROM pg_catalog.pg_roles r
                WHERE r.rolname=current_user"""
        )
        if user is None:
            result["status"] = "OWNER_ROLE_NOT_FOUND"
            return result
        result["database_role"] = str(user["username"])
        result["role_creation_allowed"] = (
            user["can_create_role"] is True or user["is_superuser"] is True
        )
        role = await conn.fetchrow(
            """SELECT rolname,rolcanlogin,rolsuper,rolcreaterole,
                      rolcreatedb,rolreplication,rolbypassrls
               FROM pg_catalog.pg_roles
               WHERE rolname=$1""",
            TARGET_ROLE,
        )
        result["runtime_role_absent"] = role is None
        if role is not None:
            result["runtime_role_existing_attributes"] = {
                key: bool(role[key])
                for key in (
                    "rolcanlogin", "rolsuper", "rolcreaterole",
                    "rolcreatedb", "rolreplication", "rolbypassrls",
                )
            }

        owner_rows = await conn.fetch(
            """SELECT c.relname AS name,
                      pg_get_userbyid(c.relowner) AS owner_name
               FROM pg_catalog.pg_class c
               JOIN pg_catalog.pg_namespace n
                 ON n.oid=c.relnamespace
               WHERE n.nspname='public'
                 AND c.relkind IN ('r','p')
                 AND c.relname = ANY($1::text[])""",
            sorted(LIVE_TABLES),
        )
        table_owners = {
            str(row["name"]): str(row["owner_name"])
            for row in owner_rows
        }
        result["live_tables_present"] = len(table_owners)
        result["live_table_owner_verified"] = (
            set(table_owners) == set(LIVE_TABLES)
            and all(
                owner == result["database_role"]
                for owner in table_owners.values()
            )
        )

        seq_rows = await conn.fetch(
            """SELECT c.relname AS name,
                      pg_get_userbyid(c.relowner) AS owner_name
               FROM pg_catalog.pg_class c
               JOIN pg_catalog.pg_namespace n
                 ON n.oid=c.relnamespace
               WHERE n.nspname='public'
                 AND c.relkind='S'
                 AND c.relname = ANY($1::text[])""",
            sorted(LIVE_SEQUENCES),
        )
        sequence_owners = {
            str(row["name"]): str(row["owner_name"])
            for row in seq_rows
        }
        result["live_sequences_present"] = len(sequence_owners)
        result["live_sequence_owner_verified"] = (
            set(sequence_owners) == set(LIVE_SEQUENCES)
            and all(
                owner == result["database_role"]
                for owner in sequence_owners.values()
            )
        )
        if not result["live_table_owner_verified"]:
            result["status"] = "LIVE_TABLE_OWNER_UNVERIFIED"
        elif not result["live_sequence_owner_verified"]:
            result["status"] = "LIVE_SEQUENCE_OWNER_UNVERIFIED"
        elif not result["runtime_role_absent"]:
            result["status"] = "TARGET_ROLE_EXISTS_REVIEW_MANUALLY"
        elif not result["role_creation_allowed"]:
            result["status"] = "OWNER_LACKS_CREATE_ROLE"
        else:
            result["status"] = "DIRECT_ROLE_CREATION_PREFLIGHT_PASSED"
        return result
    except Exception as exc:
        # Never print raw errors or DSNs.
        result["status"] = "ROLE_PREFLIGHT_READ_FAILED"
        result["failure_type"] = type(exc).__name__
        return result


async def _main(*, prompt_dsn: bool) -> None:
    if os.getenv("T100_EXECUTION_MODE", "paper").strip().lower() != "paper":
        raise RuntimeError("ROLE_PREFLIGHT_REQUIRES_PAPER_MODE")
    dsn = (
        getpass.getpass("Existing owner Render PostgreSQL URL (hidden): ")
        if prompt_dsn else os.getenv("DATABASE_URL", "")
    )
    if not dsn:
        raise RuntimeError("OWNER_DATABASE_URL_REQUIRED")
    import asyncpg
    try:
        conn = await asyncpg.connect(
            dsn=dsn, ssl="require", timeout=10, command_timeout=10,
        )
    except Exception as exc:
        print(json.dumps({
            "status": "DATABASE_CONNECTION_FAILED",
            "failure_type": type(exc).__name__,
            "live_authorization": False,
        }))
        return
    try:
        async with conn.transaction(readonly=True):
            report = await inspect_role_setup_preconditions(conn)
        print(json.dumps(report, indent=2, sort_keys=True))
        print("READ ONLY. NO USERS CREATED. NO GRANTS. NO MEXC ORDERS.")
    finally:
        await conn.close()


def cli() -> None:
    parser = argparse.ArgumentParser(
        description="Read-only PostgreSQL role provisioning preflight"
    )
    parser.add_argument(
        "--prompt-dsn", action="store_true",
        help="Prompt for the owner database URL without echo or export",
    )
    args = parser.parse_args()
    asyncio.run(_main(prompt_dsn=args.prompt_dsn))


if __name__ == "__main__":
    cli()
