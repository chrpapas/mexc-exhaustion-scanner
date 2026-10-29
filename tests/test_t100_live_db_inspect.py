"""Offline unit checks of fail-closed DB commissioning inspector."""
import unittest
from unittest.mock import AsyncMock, patch
from app.t100_live_db_inspect import (
    inspect_live_database, LEASE_TABLE, STRATEGY_TABLE_COLUMNS,
    LEASE_COLUMNS, REQUIRED_STRATEGY_INDEXES,
    ALLOWED_STRATEGY_UPDATES, IMMUTABLE_STRATEGY_COLUMNS,
)
from app.t100_live_schema_gate import LIVE_TABLE_COLUMNS


class FakeConnection:
    def __init__(self, *, readonly="on", present=None, error=False,
                 database="mexc_exhaustion"):
        self.readonly = readonly
        self.database = database
        self.present = set(present or [])
        self.error = error
        self.calls = []

    async def fetchval(self, query, *args):
        self.calls.append(query)
        if self.error:
            raise TimeoutError()
        if "current_database()" in query:
            return self.database
        return self.readonly

    async def fetch(self, query, *args):
        self.calls.append(query)
        if self.error:
            raise TimeoutError()
        return [{"table_name": n} for n in sorted(self.present)]


class CompleteConnection(FakeConnection):
    def __init__(self, *, allow_immutable_update=False):
        super().__init__(present=(
            set(LIVE_TABLE_COLUMNS) | set(STRATEGY_TABLE_COLUMNS)
            | {LEASE_TABLE}
        ))
        self.allow_immutable_update = allow_immutable_update

    async def fetch(self, query, *args):
        self.calls.append(query)
        if "information_schema.tables" in query:
            return [{"table_name": name} for name in self.present]
        if "information_schema.columns" in query:
            cols = dict(STRATEGY_TABLE_COLUMNS)
            cols[LEASE_TABLE] = LEASE_COLUMNS
            return [
                {"table_name": name, "column_name": col}
                for name, required in cols.items()
                for col in required
            ]
        if "FROM pg_indexes" in query:
            return [
                {"indexname": name}
                for name in REQUIRED_STRATEGY_INDEXES
            ]
        if "has_table_privilege" in query:
            return [
                {
                    "table_name": name,
                    "can_select": True, "can_insert": True,
                    "can_update": False,
                    "can_delete": False, "can_truncate": False,
                }
                for name in (set(STRATEGY_TABLE_COLUMNS) | {LEASE_TABLE})
            ]
        if "has_column_privilege" in query:
            rows = []
            for table, column in zip(args[0], args[1]):
                can_update = column in ALLOWED_STRATEGY_UPDATES[table]
                if self.allow_immutable_update and column in (
                    IMMUTABLE_STRATEGY_COLUMNS[table]
                ):
                    can_update = True
                rows.append({
                    "table_name": table, "column_name": column,
                    "can_update": can_update,
                })
            return rows
        raise AssertionError("UNEXPECTED_READ_ONLY_QUERY")

    async def fetchrow(self, query, *args):
        self.calls.append(query)
        return {"can_usage": True, "can_select": True, "can_update": False}


class DBInspectionTests(unittest.IsolatedAsyncioTestCase):
    async def test_no_live_tables_reports_missing_without_mutating(self):
        c = FakeConnection()
        r = await inspect_live_database(c)
        self.assertEqual(r["live_tables_present"], 0)
        self.assertFalse(r["schema_gate_verified"])
        self.assertFalse(r["unresolved_mutations_absent"])
        self.assertFalse(r["live_authorization"])
        self.assertTrue(r["read_only"])
        self.assertFalse(r["migrations_applied_by_inspection"])
        self.assertIn(LEASE_TABLE, r["missing_table_names"])
        self.assertTrue(all(q.strip().upper().startswith("SELECT") for q in c.calls))

    async def test_refuses_non_readonly_transaction(self):
        c = FakeConnection(readonly="off")
        r = await inspect_live_database(c)
        self.assertEqual(r["status"], "NOT_IN_READ_ONLY_TRANSACTION")
        self.assertEqual(len(c.calls), 1)

    async def test_rejects_wrong_database_before_table_reads(self):
        c = FakeConnection(database="some_other_db")
        r = await inspect_live_database(c)
        self.assertEqual(r["status"], "WRONG_DATABASE_IDENTITY")
        self.assertFalse(r["live_authorization"])
        self.assertFalse(r["database_name_verified"])
        self.assertEqual(len(c.calls), 2)

    async def test_missing_vol30_tables_blocks_full_strategy_readiness(self):
        c = FakeConnection(present=set(LIVE_TABLE_COLUMNS) | {LEASE_TABLE})
        r = await inspect_live_database(c)
        self.assertIn(
            "t100_live_vol30_parents", r["missing_table_names"]
        )
        self.assertIn(
            "t100_live_vol30_children", r["missing_table_names"]
        )
        self.assertFalse(r["strategy_storage_columns_verified"])
        self.assertEqual(
            r["status"], "LIVE_DATABASE_MIGRATION_PREREQUISITES_MISSING"
        )

    async def test_full_strategy_table_and_privilege_check_passes(self):
        c = CompleteConnection()
        with (
            patch(
                "app.t100_live_db_inspect.assess_live_schema_and_privileges",
                new_callable=AsyncMock,
                return_value={"durable_schema_deployed_verified": True},
            ),
            patch(
                "app.t100_live_db_inspect.assess_unresolved_live_state",
                new_callable=AsyncMock,
                return_value={"unresolved_mutations_absent": True},
            ),
        ):
            r = await inspect_live_database(c)
        self.assertEqual(
            r["status"], "LIVE_DATABASE_READ_ONLY_PRECHECK_PASSED"
        )
        self.assertTrue(r["strategy_storage_columns_verified"])
        self.assertTrue(r["strategy_storage_indexes_verified"])
        self.assertTrue(r["strategy_storage_privileges_verified"])
        self.assertFalse(r["live_authorization"])

    async def test_immutable_column_update_permission_blocks(self):
        c = CompleteConnection(allow_immutable_update=True)
        with (
            patch(
                "app.t100_live_db_inspect.assess_live_schema_and_privileges",
                new_callable=AsyncMock,
                return_value={"durable_schema_deployed_verified": True},
            ),
            patch(
                "app.t100_live_db_inspect.assess_unresolved_live_state",
                new_callable=AsyncMock,
                return_value={"unresolved_mutations_absent": True},
            ),
        ):
            r = await inspect_live_database(c)
        self.assertEqual(
            r["status"], "FULL_STRATEGY_STORAGE_PRIVILEGES_UNVERIFIED"
        )
        self.assertFalse(r["strategy_storage_privileges_verified"])
        self.assertFalse(r["live_authorization"])

    async def test_failure_is_redacted_fail_closed(self):
        c = FakeConnection(error=True)
        r = await inspect_live_database(c)
        self.assertEqual(r["status"], "LIVE_DATABASE_INSPECTION_FAILED")
        self.assertEqual(r["failure_type"], "TimeoutError")
        self.assertNotIn("DATABASE_URL", str(r))
        self.assertFalse(r["live_authorization"])

    async def test_lease_missing_reported(self):
        c = FakeConnection(present=[])
        r = await inspect_live_database(c)
        self.assertEqual(
            len(r["missing_table_names"]),
            len(LIVE_TABLE_COLUMNS) + len(STRATEGY_TABLE_COLUMNS) + 1,
        )


if __name__ == "__main__":
    unittest.main()
