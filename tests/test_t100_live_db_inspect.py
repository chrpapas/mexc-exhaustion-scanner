"""Offline unit checks of fail-closed DB commissioning inspector."""
import unittest
from app.t100_live_db_inspect import (
    inspect_live_database, LEASE_TABLE, STRATEGY_TABLE_COLUMNS,
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
