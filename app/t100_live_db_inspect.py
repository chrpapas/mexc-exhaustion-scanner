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

# These research/slicing tables are present in migration 028, not in the
# mutable intent/checkpoint accounting privilege gate (027/029).
STRATEGY_TABLE_COLUMNS: dict[str, frozenset[str]] = {
    "t100_live_vol30_parents": frozenset({
        "signal_id", "symbol", "tier", "admitted_at",
        "admitted_equity_usdt", "target_notional_usdt", "status",
        "created_at",
    }),
    "t100_live_vol30_children": frozenset({
        "id", "signal_id", "child_seq", "symbol", "external_oid",
        "turnover_window_end", "prior30_turnover_usdt",
        "reserved_notional_usdt", "volume_contracts",
        "filled_notional_usdt", "state", "exchange_order_id",
        "created_at", "updated_at",
    }),
}
LEASE_TABLE = "t100_live_executor_leases"
LEASE_COLUMNS = frozenset({
    "lease_name", "holder_id", "fencing_token", "lease_expires_at",
    "updated_at",
})
REQUIRED_STRATEGY_INDEXES = frozenset({
    "t100_live_vol30_active_symbol",
    "t100_live_vol30_one_inflight_child",
    "t100_live_vol30_symbol_bucket",
    "idx_t100_live_executor_lease_expiry",
})
EXPECTED_DATABASE = "mexc_exhaustion"

# Only these columns are allowed to change after an initial INSERT.
ALLOWED_STRATEGY_UPDATES: dict[str, frozenset[str]] = {
    "t100_live_vol30_parents": frozenset({"status"}),
    "t100_live_vol30_children": frozenset({
        "state", "filled_notional_usdt", "exchange_order_id", "updated_at",
    }),
    LEASE_TABLE: frozenset({
        "holder_id", "fencing_token", "lease_expires_at", "updated_at",
    }),
}
IMMUTABLE_STRATEGY_COLUMNS: dict[str, frozenset[str]] = {
    "t100_live_vol30_parents": frozenset({
        "signal_id", "symbol", "admitted_equity_usdt", "target_notional_usdt",
    }),
    "t100_live_vol30_children": frozenset({
        "id", "signal_id", "child_seq", "symbol", "external_oid",
        "reserved_notional_usdt", "volume_contracts",
    }),
    LEASE_TABLE: frozenset({"lease_name"}),
}
STRATEGY_SEQUENCE = "t100_live_vol30_children_id_seq"


async def inspect_live_database(conn: Any) -> dict[str, Any]:
    """Summarize current live tables, privileges and unresolved states."""
    result: dict[str, Any] = {
        "read_only": True,
        "mexc_api_used": False,
        "orders_sent": False,
        "migrations_applied_by_inspection": False,
        "live_authorization": False,
        "tables_expected": len(LIVE_TABLE_COLUMNS) + len(STRATEGY_TABLE_COLUMNS) + 1,
        "database_name_verified": False,
        "strategy_storage_columns_verified": False,
        "strategy_storage_indexes_verified": False,
        "strategy_storage_privileges_verified": False,
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
        # Do not inspect the wrong database: its similarly named tables can
        # otherwise produce an unsafe, misleading readiness result.
        database = await conn.fetchval("SELECT current_database()")
        result["database_name_verified"] = database == EXPECTED_DATABASE
        if not result["database_name_verified"]:
            result["status"] = "WRONG_DATABASE_IDENTITY"
            return result

        names = set(LIVE_TABLE_COLUMNS) | set(STRATEGY_TABLE_COLUMNS) | {
            LEASE_TABLE
        }
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

        extra_tables = set(STRATEGY_TABLE_COLUMNS) | {LEASE_TABLE}
        if extra_tables <= present:
            columns = await conn.fetch(
                """SELECT table_name,column_name FROM information_schema.columns
                   WHERE table_schema='public'
                     AND table_name = ANY($1::text[])""",
                sorted(extra_tables),
            )
            actual_columns: dict[str, set[str]] = {
                name: set() for name in extra_tables
            }
            for row in columns:
                name = row.get("table_name")
                if name in actual_columns:
                    actual_columns[name].add(str(row.get("column_name") or ""))
            required_columns = dict(STRATEGY_TABLE_COLUMNS)
            required_columns[LEASE_TABLE] = LEASE_COLUMNS
            result["strategy_storage_columns_verified"] = all(
                wanted <= actual_columns[name]
                for name, wanted in required_columns.items()
            )
            indexes = await conn.fetch(
                """SELECT indexname FROM pg_indexes
                   WHERE schemaname='public'
                     AND indexname = ANY($1::text[])""",
                sorted(REQUIRED_STRATEGY_INDEXES),
            )
            result["strategy_storage_indexes_verified"] = (
                REQUIRED_STRATEGY_INDEXES
                <= {str(row["indexname"]) for row in indexes}
            )
            if (
                result["strategy_storage_columns_verified"]
                and result["strategy_storage_indexes_verified"]
            ):
                table_privileges = await conn.fetch(
                    """SELECT table_name,
                              has_table_privilege(current_user,
                                  format('public.%I',table_name),'SELECT')
                                  AS can_select,
                              has_table_privilege(current_user,
                                  format('public.%I',table_name),'INSERT')
                                  AS can_insert,
                              has_table_privilege(current_user,
                                  format('public.%I',table_name),'UPDATE')
                                  AS can_update,
                              has_table_privilege(current_user,
                                  format('public.%I',table_name),'DELETE')
                                  AS can_delete,
                              has_table_privilege(current_user,
                                  format('public.%I',table_name),'TRUNCATE')
                                  AS can_truncate
                       FROM unnest($1::text[]) AS table_name""",
                    sorted(extra_tables),
                )
                table_flags = {
                    str(row["table_name"]): row for row in table_privileges
                }
                tables_ok = (
                    set(table_flags) == extra_tables
                    and all(
                        row.get("can_select") is True
                        and row.get("can_insert") is True
                        and row.get("can_update") is False
                        and row.get("can_delete") is False
                        and row.get("can_truncate") is False
                        for row in table_flags.values()
                    )
                )
                pairs = sorted(
                    (table, column)
                    for table, selected in ALLOWED_STRATEGY_UPDATES.items()
                    for column in (
                        selected | IMMUTABLE_STRATEGY_COLUMNS[table]
                    )
                )
                column_privileges = await conn.fetch(
                    """SELECT table_name,column_name,
                              has_column_privilege(
                                  current_user,
                                  format('public.%I',table_name),
                                  column_name,'UPDATE'
                              ) AS can_update
                       FROM unnest($1::text[], $2::text[])
                         AS t(table_name,column_name)""",
                    [table for table, _ in pairs],
                    [column for _, column in pairs],
                )
                observed_column_flags = {
                    (str(row["table_name"]), str(row["column_name"])):
                        row.get("can_update")
                    for row in column_privileges
                }
                columns_ok = (
                    set(observed_column_flags) == set(pairs)
                    and all(
                        observed_column_flags[(table, column)]
                        is (column in ALLOWED_STRATEGY_UPDATES[table])
                        for table, column in pairs
                    )
                )
                seq = await conn.fetchrow(
                    """SELECT
                         has_sequence_privilege(current_user,
                             'public.t100_live_vol30_children_id_seq',
                             'USAGE') AS can_usage,
                         has_sequence_privilege(current_user,
                             'public.t100_live_vol30_children_id_seq',
                             'SELECT') AS can_select,
                         has_sequence_privilege(current_user,
                             'public.t100_live_vol30_children_id_seq',
                             'UPDATE') AS can_update"""
                )
                sequence_ok = (
                    seq is not None
                    and seq.get("can_usage") is True
                    and seq.get("can_select") is True
                    and seq.get("can_update") is False
                )
                result["strategy_storage_privileges_verified"] = bool(
                    tables_ok and columns_ok and sequence_ok
                )

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
        elif not (
            result["strategy_storage_columns_verified"]
            and result["strategy_storage_indexes_verified"]
        ):
            result["status"] = "FULL_STRATEGY_STORAGE_SHAPE_UNVERIFIED"
        elif not result["strategy_storage_privileges_verified"]:
            result["status"] = "FULL_STRATEGY_STORAGE_PRIVILEGES_UNVERIFIED"
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
