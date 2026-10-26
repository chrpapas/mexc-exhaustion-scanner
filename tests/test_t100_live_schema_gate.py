"""Offline live durable schema / runtime privilege gate tests."""
import unittest

from app.t100_live_schema_gate import (
    APPEND_ONLY_TABLES,
    LIVE_TABLE_COLUMNS,
    MUTABLE_TABLES,
    REQUIRED_INDEXES,
    assess_live_schema_and_privileges,
)


class Pool:
    def __init__(self):
        self.fail = False
        self.tables = tuple(sorted(LIVE_TABLE_COLUMNS))
        self.columns = {
            table: set(cols)
            for table, cols in LIVE_TABLE_COLUMNS.items()
        }
        self.indexes = set(REQUIRED_INDEXES)
        self.privileges = {}
        for table in self.tables:
            self.privileges[table] = {
                "can_select": True,
                "can_insert": True,
                "can_update": table in MUTABLE_TABLES,
                "can_delete": False,
                "can_truncate": False,
            }
        self.calls = 0

    async def fetch(self, sql, values):
        self.calls += 1
        if self.fail:
            raise ConnectionError("db unavailable")
        if "information_schema.tables" in sql:
            return [{"table_name": x} for x in self.tables]
        if "information_schema.columns" in sql:
            return [
                {"table_name": table, "column_name": column}
                for table, cols in self.columns.items()
                for column in cols
            ]
        if "pg_indexes" in sql:
            return [{"indexname": x} for x in self.indexes]
        if "has_table_privilege" in sql:
            return [
                {"table_name": table, **self.privileges[table]}
                for table in self.tables
            ]
        raise AssertionError("unexpected query")


class SchemaGateTests(unittest.IsolatedAsyncioTestCase):
    async def test_exact_schema_and_least_privilege_pass(self):
        p = Pool()
        r = await assess_live_schema_and_privileges(p)
        self.assertTrue(r["durable_schema_deployed_verified"])
        self.assertTrue(r["append_only_tables_insert_select_only"])
        self.assertFalse(r["live_authorization"])
        self.assertEqual(
            r["status"],
            "LIVE_DURABLE_SCHEMA_AND_RUNTIME_PRIVILEGES_VERIFIED",
        )

    async def test_missing_table_blocks(self):
        p = Pool()
        p.tables = p.tables[:-1]
        r = await assess_live_schema_and_privileges(p)
        self.assertFalse(r["durable_schema_deployed_verified"])
        self.assertEqual(r["status"], "LIVE_SCHEMA_TABLES_MISSING")

    async def test_missing_column_blocks(self):
        p = Pool()
        table = next(iter(LIVE_TABLE_COLUMNS))
        p.columns[table].pop()
        r = await assess_live_schema_and_privileges(p)
        self.assertEqual(r["status"], "LIVE_SCHEMA_COLUMNS_MISSING")

    async def test_missing_index_blocks(self):
        p = Pool()
        p.indexes.pop()
        r = await assess_live_schema_and_privileges(p)
        self.assertEqual(r["status"], "LIVE_SCHEMA_INDEXES_MISSING")

    async def test_append_only_update_delete_or_truncate_blocks(self):
        table = next(iter(APPEND_ONLY_TABLES))
        for privilege in ("can_update", "can_delete", "can_truncate"):
            p = Pool()
            p.privileges[table][privilege] = True
            with self.subTest(privilege=privilege):
                r = await assess_live_schema_and_privileges(p)
                self.assertFalse(r["durable_schema_deployed_verified"])
                self.assertEqual(
                    r["status"],
                    "LIVE_DURABLE_SCHEMA_PRIVILEGE_MISMATCH",
                )

    async def test_mutable_tables_require_update(self):
        p = Pool()
        table = next(iter(MUTABLE_TABLES))
        p.privileges[table]["can_update"] = False
        r = await assess_live_schema_and_privileges(p)
        self.assertFalse(r["durable_schema_deployed_verified"])

    async def test_delete_and_truncate_are_forbidden_everywhere(self):
        for privilege in ("can_delete", "can_truncate"):
            p = Pool()
            table = next(iter(MUTABLE_TABLES))
            p.privileges[table][privilege] = True
            r = await assess_live_schema_and_privileges(p)
            self.assertFalse(r["durable_schema_deployed_verified"])

    async def test_database_failure_fails_closed(self):
        p = Pool()
        p.fail = True
        r = await assess_live_schema_and_privileges(p)
        self.assertFalse(r["durable_schema_deployed_verified"])
        self.assertEqual(r["status"], "LIVE_SCHEMA_READ_FAILED")


if __name__ == "__main__":
    unittest.main()
