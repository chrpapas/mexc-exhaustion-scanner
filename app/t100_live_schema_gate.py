"""Read-only PostgreSQL schema and privilege gate for T100 live state.

This verifier does not run migrations. It checks that the independent live
tables exist with the columns required by the application and that the runtime
role cannot UPDATE/DELETE/TRUNCATE append-only evidence tables.

A passing result is a prerequisite for durable_schema_deployed_verified.
"""
from __future__ import annotations

from typing import Any


LIVE_TABLE_COLUMNS: dict[str, frozenset[str]] = {
    "t100_live_order_intents": frozenset({
        "id", "signal_id", "symbol", "external_oid", "reference_price",
        "notional_usdt", "volume_contracts", "stop_price", "state",
        "exchange_order_id", "failure_reason", "created_at", "updated_at",
    }),
    "t100_live_position_checkpoints": frozenset({
        "position_id", "symbol", "external_oid", "exchange_order_id",
        "contracts", "entry_price", "stop_order_id", "stop_price",
        "best_profit_pct", "armed", "lifecycle_state",
        "checkpoint_version", "last_write_reason", "created_at",
        "updated_at", "verified_at",
    }),
    "t100_live_position_checkpoint_revisions": frozenset({
        "revision_id", "position_id", "checkpoint_version", "write_reason",
        "symbol", "external_oid", "exchange_order_id", "contracts",
        "entry_price", "stop_order_id", "stop_price", "best_profit_pct",
        "armed", "lifecycle_state", "verified_at", "created_at",
    }),
    "t100_live_stop_amendments": frozenset({
        "id", "external_id", "symbol", "position_id", "stop_order_id",
        "previous_stop", "proposed_stop", "verified_stop_order_id", "state",
        "created_at", "updated_at",
    }),
    "t100_live_order_fill_sets": frozenset({
        "order_id", "position_id", "symbol", "side", "order_state",
        "deal_volume", "average_price", "fee_currency", "fill_count",
        "fill_digest_sha256", "strategy_fully_filled_order_complete",
        "verified_at",
    }),
    "t100_live_order_fills": frozenset({
        "trade_id", "order_id", "position_id", "side", "contracts", "price",
        "fee_usdt", "timestamp_ms", "category", "created_at",
    }),
    "t100_live_position_funding_sets": frozenset({
        "position_id", "total_count", "funding_digest_sha256",
        "economics_verified", "double_collection_finality_verified",
        "verified_at",
    }),
    "t100_live_funding_events": frozenset({
        "event_id", "position_id", "symbol", "position_type",
        "position_value", "funding_cashflow_usdt", "rate",
        "settle_time_ms", "created_at",
    }),
    "t100_live_final_position_accounting": frozenset({
        "position_id", "entry_order_id", "close_order_id", "stop_order_id",
        "opened_contracts", "closed_contracts", "open_notional_usdt",
        "close_notional_usdt", "realized_gross_usdt", "fees_usdt",
        "funding_cashflow_usdt", "realized_net_usdt", "verified_at",
    }),
}

MUTABLE_TABLES = frozenset({
    "t100_live_order_intents",
    "t100_live_position_checkpoints",
    "t100_live_stop_amendments",
})

APPEND_ONLY_TABLES = frozenset(LIVE_TABLE_COLUMNS) - MUTABLE_TABLES

REQUIRED_INDEXES = frozenset({
    "t100_live_order_intents_state_idx",
    "uq_t100_live_checkpoint_symbol_open",
    "idx_t100_checkpoint_revisions_position",
    "idx_t100_stop_amendments_unresolved",
    "uq_t100_stop_amendments_one_pending",
    "idx_t100_live_order_fills_position_time",
    "idx_t100_live_funding_position_time",
})


async def assess_live_schema_and_privileges(pool: Any) -> dict[str, Any]:
    result: dict[str, Any] = {
        "required_tables": len(LIVE_TABLE_COLUMNS),
        "tables_present": 0,
        "required_columns_present": False,
        "required_indexes_present": False,
        "mutable_tables_have_required_write_privileges": False,
        "append_only_tables_insert_select_only": False,
        "delete_privilege_absent_on_all_live_tables": False,
        "truncate_privilege_absent_on_all_live_tables": False,
        "durable_schema_deployed_verified": False,
        "live_authorization": False,
    }
    if pool is None:
        result["status"] = "LIVE_SCHEMA_POOL_MISSING"
        return result

    names = tuple(sorted(LIVE_TABLE_COLUMNS))
    try:
        table_rows = await pool.fetch(
            """SELECT table_name
               FROM information_schema.tables
               WHERE table_schema='public'
                 AND table_name = ANY($1::text[])""",
            list(names),
        )
        column_rows = await pool.fetch(
            """SELECT table_name,column_name
               FROM information_schema.columns
               WHERE table_schema='public'
                 AND table_name = ANY($1::text[])""",
            list(names),
        )
        index_rows = await pool.fetch(
            """SELECT indexname
               FROM pg_indexes
               WHERE schemaname='public'
                 AND indexname = ANY($1::text[])""",
            list(sorted(REQUIRED_INDEXES)),
        )
        privilege_rows = await pool.fetch(
            """SELECT table_name,
                      has_table_privilege(
                          current_user,
                          format('public.%I',table_name),
                          'SELECT'
                      ) AS can_select,
                      has_table_privilege(
                          current_user,
                          format('public.%I',table_name),
                          'INSERT'
                      ) AS can_insert,
                      has_table_privilege(
                          current_user,
                          format('public.%I',table_name),
                          'UPDATE'
                      ) AS can_update,
                      has_table_privilege(
                          current_user,
                          format('public.%I',table_name),
                          'DELETE'
                      ) AS can_delete,
                      has_table_privilege(
                          current_user,
                          format('public.%I',table_name),
                          'TRUNCATE'
                      ) AS can_truncate
               FROM unnest($1::text[]) AS table_name""",
            list(names),
        )
    except Exception as exc:
        result["status"] = "LIVE_SCHEMA_READ_FAILED"
        result["failure_type"] = type(exc).__name__
        return result

    present_tables = {
        str(row["table_name"])
        for row in table_rows
        if row.get("table_name") is not None
    }
    result["tables_present"] = len(present_tables)
    if present_tables != set(names):
        result["status"] = "LIVE_SCHEMA_TABLES_MISSING"
        return result

    columns: dict[str, set[str]] = {name: set() for name in names}
    for row in column_rows:
        table = str(row.get("table_name") or "")
        column = str(row.get("column_name") or "")
        if table in columns and column:
            columns[table].add(column)
    columns_ok = all(
        required <= columns[table]
        for table, required in LIVE_TABLE_COLUMNS.items()
    )
    result["required_columns_present"] = columns_ok
    if not columns_ok:
        result["status"] = "LIVE_SCHEMA_COLUMNS_MISSING"
        return result

    indexes = {
        str(row["indexname"])
        for row in index_rows
        if row.get("indexname") is not None
    }
    indexes_ok = REQUIRED_INDEXES <= indexes
    result["required_indexes_present"] = indexes_ok
    if not indexes_ok:
        result["status"] = "LIVE_SCHEMA_INDEXES_MISSING"
        return result

    privileges: dict[str, dict[str, bool]] = {}
    for row in privilege_rows:
        table = str(row.get("table_name") or "")
        if table not in LIVE_TABLE_COLUMNS:
            continue
        privileges[table] = {
            "select": row.get("can_select") is True,
            "insert": row.get("can_insert") is True,
            "update": row.get("can_update") is True,
            "delete": row.get("can_delete") is True,
            "truncate": row.get("can_truncate") is True,
        }
    if set(privileges) != set(names):
        result["status"] = "LIVE_SCHEMA_PRIVILEGE_ROWS_INCOMPLETE"
        return result

    mutable_ok = all(
        privileges[table]["select"]
        and privileges[table]["insert"]
        and privileges[table]["update"]
        for table in MUTABLE_TABLES
    )
    append_only_ok = all(
        privileges[table]["select"]
        and privileges[table]["insert"]
        and not privileges[table]["update"]
        and not privileges[table]["delete"]
        and not privileges[table]["truncate"]
        for table in APPEND_ONLY_TABLES
    )
    no_delete = all(
        not privileges[table]["delete"] for table in names
    )
    no_truncate = all(
        not privileges[table]["truncate"] for table in names
    )

    result["mutable_tables_have_required_write_privileges"] = mutable_ok
    result["append_only_tables_insert_select_only"] = append_only_ok
    result["delete_privilege_absent_on_all_live_tables"] = no_delete
    result["truncate_privilege_absent_on_all_live_tables"] = no_truncate
    result["durable_schema_deployed_verified"] = all((
        mutable_ok,
        append_only_ok,
        no_delete,
        no_truncate,
    ))
    result["status"] = (
        "LIVE_DURABLE_SCHEMA_AND_RUNTIME_PRIVILEGES_VERIFIED"
        if result["durable_schema_deployed_verified"]
        else "LIVE_DURABLE_SCHEMA_PRIVILEGE_MISMATCH"
    )
    return result
