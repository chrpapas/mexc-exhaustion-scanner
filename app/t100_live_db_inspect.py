"""Read-only, MEXC-independent commissioning inspection of live Postgres.

Usage (from a trusted host already allowed by Render):
    T100_EXECUTION_MODE=paper python -m app.t100_live_db_inspect

Reads DATABASE_URL from the process environment, never prints it, and opens
one TLS-required connection in an explicitly READ ONLY transaction. Does not
run migrations, touch MEXC or submit any orders.
"""
from __future__ import annotations

import asyncio
import json
import os
from typing import Any

from app.t100_live_schema_gate import (
    LIVE_TABLE_COLUMNS,
    assess_live_schema_and_privileges,
)
from app.t100_live_db_state_gate import assess_unresolved_live_state

LEASE_TABLE = "t100_live_executor_leases"


async def inspect_live_database(conn: Any) -> dict[str, Any]:
    """Summarize current live tables, privileges and unresolved states."""
    result: dict[str, Any] = {
        "read_only": True,
        "mexc_api_used": False,
        "orders_sent": False,
        "migrations_applied_by_inspection": False,
        "live_authorization": False,
        "tables_expected": len(LIVE_TABLE_COLUMNS) + 1,
        "live_tables_present": 0,
        "lease_table_present": False,
        "schema_gate_verified": False,
        "unresolved_mutations_absent": False,
    }
    try:
        readonly = await conn.fetchval(
            "SELECT current_setting('transaction_read_only')"
        )
        if readonly != "on":
            result["status"] = "NOT_IN_READ_ONLY_TRANSACTION"
            return result
        names = set(LIVE_TABLE_COLUMNS) | {LEASE_TABLE}
        rows = await conn.fetch(
            """SELECT table_name FROM information_schema.tables
               WHERE table_schema='public'
                 AND table_name = ANY($1::text[])""",
            sorted(names),
        )
        present = {
            str(row["table_name"])
            for row in rows
            if row.get("table_name") in names
        }
        result["live_tables_present"] = len(present)
        result["lease_table_present"] = LEASE_TABLE in present
        result["missing_table_names"] = sorted(names - present)

        if set(LIVE_TABLE_COLUMNS) <= present:
            schema = await assess_live_schema_and_privileges(conn)
            state = await assess_unresolved_live_state(conn)
            result["schema"] = schema
            result["state"] = state
            result["schema_gate_verified"] = (
                schema.get("durable_schema_deployed_verified") is True
            )
            result["unresolved_mutations_absent"] = (
                state.get("unresolved_mutations_absent") is True
            )
        else:
            result["schema"] = {"status": "LIVE_TABLES_NOT_YET_PRESENT"}
            result["state"] = {"status": "NOT_RUN_MISSING_LIVE_TABLES"}

        if result["missing_table_names"]:
            result["status"] = "LIVE_DATABASE_MIGRATION_PREREQUISITES_MISSING"
        elif not result["schema_gate_verified"]:
            result["status"] = "LIVE_SCHEMA_OR_RUNTIME_PRIVILEGES_NOT_VERIFIED"
        elif not result["unresolved_mutations_absent"]:
            result["status"] = "LIVE_DURABLE_STATE_NOT_CLEAN"
        else:
            result["status"] = "LIVE_DATABASE_READ_ONLY_PRECHECK_PASSED"
        return result
    except Exception as exc:
        result["status"] = "LIVE_DATABASE_INSPECTION_FAILED"
        result["failure_type"] = type(exc).__name__
        return result


async def _main() -> None:
    if os.getenv("T100_EXECUTION_MODE", "paper").lower() != "paper":
        raise RuntimeError("DB_INSPECTION_REQUIRES_PAPER_MODE")
    dsn = os.getenv("DATABASE_URL") or ""
    if not dsn:
        raise RuntimeError("DATABASE_URL_REQUIRED")

    import asyncpg

    # Explicit ssl=require avoids the Render FATAL: SSL/TLS required error.
    # Never emit the DSN or raw driver exception (may contain credentials).
    try:
        conn = await asyncpg.connect(
            dsn=dsn,
            ssl="require",
            timeout=10,
            command_timeout=10,
        )
    except Exception as exc:
        print(json.dumps({
            "status": "DATABASE_CONNECTION_FAILED",
            "failure_type": type(exc).__name__,
            "live_authorization": False,
        }, sort_keys=True))
        return

    try:
        async with conn.transaction(readonly=True):
            report = await inspect_live_database(conn)
        print(json.dumps(report, indent=2, sort_keys=True, default=str))
        print("READ ONLY. NO MIGRATIONS. NO MEXC REQUESTS. NO ORDERS.")
    finally:
        await conn.close()


def cli() -> None:
    asyncio.run(_main())


if __name__ == "__main__":
    cli()
