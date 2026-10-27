"""Offline safety tests for a manual-only, atomic create-only DB migration."""
import tempfile
import unittest
from pathlib import Path

from app.t100_live_db_migration import (
    LIVE_TABLES,
    LIVE_COLUMNS,
    INDEXES,
    SEQUENCES,
    MIGRATIONS,
    LiveMigrationBlocked,
    _git_blob_sha,
    classify_live_namespace,
    inspect_or_apply,
    load_pinned_migrations,
)


class Transaction:
    def __init__(self, conn, readonly):
        self.conn = conn
        self.readonly = readonly

    async def __aenter__(self):
        self.conn.transaction_readonly = self.readonly
        self.conn.tx_started = True
        return self.conn

    async def __aexit__(self, exc_type, exc, tb):
        self.conn.tx_rolled_back = exc_type is not None
        self.conn.tx_committed = exc_type is None
        return False


class FakeConn:
    def __init__(self, *, existing=None, database="mexc_exhaustion",
                 lock=True, invalid_structure=False):
        self.present = set(existing or [])
        self.database = database
        self.lock = lock
        self.invalid_structure = invalid_structure
        self.executed = []
        self.migration_statements = 0
        self.tx_started = False
        self.tx_rolled_back = False
        self.tx_committed = False
        self.transaction_readonly = None

    def transaction(self, readonly=True):
        return Transaction(self, readonly)

    async def execute(self, sql, *args):
        self.executed.append(sql)
        if "CREATE TABLE" in sql:
            self.migration_statements += 1
            if self.migration_statements == len(MIGRATIONS):
                self.present = set(LIVE_TABLES)

    async def fetchval(self, sql, *args):
        if "current_database()" in sql:
            return self.database
        if "pg_try_advisory_xact_lock" in sql:
            return self.lock
        raise AssertionError("UNEXPECTED_SCALAR_READ")

    async def fetch(self, sql, *args):
        if "information_schema.tables" in sql:
            return [{"table_name": t} for t in sorted(self.present)]
        if "information_schema.columns" in sql:
            return [
                {"table_name": t, "column_name": col}
                for t, required in LIVE_COLUMNS.items()
                for col in required
                if not (self.invalid_structure and col == "checkpoint_version")
            ]
        if "pg_indexes" in sql:
            return [{"indexname": x} for x in sorted(INDEXES)]
        if "information_schema.sequences" in sql:
            return [{"sequence_name": x} for x in sorted(SEQUENCES)]
        raise AssertionError("UNEXPECTED_SQL_READ")


class MigrationSafetyTests(unittest.IsolatedAsyncioTestCase):
    def test_pinned_real_files_match_git_blob_shas(self):
        blobs = load_pinned_migrations()
        self.assertEqual(len(blobs), 4)
        for blob, (_path, sha) in zip(blobs, MIGRATIONS):
            self.assertEqual(_git_blob_sha(blob), sha)

    def test_modified_migration_file_refused(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            for path, _ in MIGRATIONS:
                dest = root / path
                dest.parent.mkdir(parents=True, exist_ok=True)
                dest.write_text("CREATE TABLE unsafe(x int);")
            with self.assertRaisesRegex(
                LiveMigrationBlocked, "PINNED_MIGRATION_CONTENT_MISMATCH"
            ):
                load_pinned_migrations(root)

    def test_empty_namespace_only_automatic_apply_case(self):
        self.assertEqual(
            classify_live_namespace(set()),
            "SAFE_EMPTY_LIVE_NAMESPACE",
        )
        self.assertEqual(
            classify_live_namespace({"t100_live_order_intents"}),
            "PARTIAL_OR_UNEXPECTED_LIVE_NAMESPACE_REVIEW_INSTEAD",
        )
        self.assertEqual(
            classify_live_namespace(set(LIVE_TABLES)),
            "ALL_LIVE_TABLES_ALREADY_PRESENT_REVIEW_INSTEAD",
        )

    async def test_plan_is_only_read_only_sql(self):
        conn = FakeConn()
        report = await inspect_or_apply(
            conn, apply=False, blobs=load_pinned_migrations()
        )
        self.assertEqual(
            report["status"], "MIGRATION_PLAN_READY_AFTER_BACKUP"
        )
        self.assertTrue(conn.transaction_readonly)
        self.assertTrue(conn.tx_committed)
        self.assertEqual(conn.migration_statements, 0)
        self.assertFalse(report["live_authorization"])

    async def test_atomic_apply_only_on_clean_namespace(self):
        conn = FakeConn()
        report = await inspect_or_apply(
            conn, apply=True, blobs=load_pinned_migrations()
        )
        self.assertEqual(report["status"], "T100_LIVE_TABLES_APPLIED_ATOMICALLY")
        self.assertEqual(report["tables_verified"], 12)
        self.assertEqual(conn.migration_statements, 4)
        self.assertFalse(conn.transaction_readonly)
        self.assertTrue(conn.tx_committed)
        self.assertFalse(report["runtime_role_privileges_verified"])
        self.assertFalse(report["live_authorization"])

    async def test_wrong_database_blocks_before_ddl(self):
        conn = FakeConn(database="wrong_database")
        with self.assertRaisesRegex(LiveMigrationBlocked, "WRONG_DATABASE"):
            await inspect_or_apply(
                conn, apply=True, blobs=load_pinned_migrations()
            )
        self.assertTrue(conn.tx_rolled_back)
        self.assertEqual(conn.migration_statements, 0)

    async def test_partial_live_tables_block_before_ddl(self):
        conn = FakeConn(existing={"t100_live_order_intents"})
        with self.assertRaisesRegex(LiveMigrationBlocked, "PARTIAL_OR"):
            await inspect_or_apply(
                conn, apply=True, blobs=load_pinned_migrations()
            )
        self.assertTrue(conn.tx_rolled_back)
        self.assertEqual(conn.migration_statements, 0)

    async def test_lock_failure_blocks_before_ddl(self):
        conn = FakeConn(lock=False)
        with self.assertRaisesRegex(LiveMigrationBlocked, "LOCK_UNAVAILABLE"):
            await inspect_or_apply(
                conn, apply=True, blobs=load_pinned_migrations()
            )
        self.assertTrue(conn.tx_rolled_back)
        self.assertEqual(conn.migration_statements, 0)

    async def test_post_ddl_shape_failure_triggers_rollback(self):
        conn = FakeConn(invalid_structure=True)
        with self.assertRaisesRegex(LiveMigrationBlocked, "COLUMNS_MISSING"):
            await inspect_or_apply(
                conn, apply=True, blobs=load_pinned_migrations()
            )
        self.assertEqual(conn.migration_statements, 4)
        self.assertTrue(conn.tx_rolled_back)


if __name__ == "__main__":
    unittest.main()
