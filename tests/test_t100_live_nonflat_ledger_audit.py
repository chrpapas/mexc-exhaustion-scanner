"""Offline-only tests for the restricted, read-only durable ledger audit."""
import unittest

from app.t100_live_nonflat_ledger_audit import (
    COUNT_FIELDS, COUNTS_SQL, inspect_nonflat_ledger,
)


def counts(**changes):
    data = dict.fromkeys(COUNT_FIELDS, 0)
    data.update(changes)
    return data


class Tx:
    def __init__(self, conn, *, isolation, readonly):
        self.conn = conn
        self.isolation = isolation
        self.readonly = readonly

    async def __aenter__(self):
        self.conn.transactions.append((
            "BEGIN", self.isolation, self.readonly
        ))
        return self

    async def __aexit__(self, exc_type, exc, tb):
        self.conn.transactions.append((
            "ROLLBACK" if exc_type else "COMMIT",
            self.isolation, self.readonly,
        ))
        return False


class FakeConnection:
    def __init__(
        self, *, database="mexc_exhaustion",
        user="t100_live_executor_runtime",
        readonly="on", isolation="repeatable read", values=None,
        fail=False,
    ):
        self.database = database
        self.user = user
        self.readonly = readonly
        self.isolation = isolation
        self.values = counts() if values is None else values
        self.fail = fail
        self.queries = []
        self.transactions = []

    def transaction(self, *, isolation, readonly):
        return Tx(self, isolation=isolation, readonly=readonly)

    async def fetchval(self, sql):
        self.queries.append(sql)
        if self.fail:
            raise RuntimeError("DO_NOT_EVER_SHOW_DSN_OR_PASSWORD")
        if "transaction_read_only" in sql:
            return self.readonly
        if "transaction_isolation" in sql:
            return self.isolation
        if "current_database()" in sql:
            return self.database
        if "current_user" in sql:
            return self.user
        raise AssertionError(sql)

    async def fetchrow(self, sql):
        self.queries.append(sql)
        self.assert_no_mutating_sql(sql)
        return self.values

    @staticmethod
    def assert_no_mutating_sql(sql):
        assert sql == COUNTS_SQL


class LedgerAuditTests(unittest.IsolatedAsyncioTestCase):
    async def test_new_empty_ledger_cannot_adopt_existing_position(self):
        conn = FakeConnection()
        r = await inspect_nonflat_ledger(conn)
        self.assertEqual(r["status"], "NO_ACTIVE_T100_DURABLE_CHECKPOINTS")
        self.assertTrue(r["restricted_runtime_role_verified"])
        self.assertTrue(r["repeatable_read_snapshot"])
        self.assertFalse(r["t100_nonflat_recovery_verified"])
        self.assertFalse(r["current_mexc_position_adopted"])
        self.assertFalse(r["live_authorization"])
        self.assertFalse(r["database_writes_sent"])
        self.assertEqual(conn.transactions[-1], ("COMMIT", "repeatable_read", True))
        self.assertTrue(all(
            q.strip().startswith("SELECT") for q in conn.queries
        ))

    async def test_active_checkpoint_candidate_is_not_recovery_proof(self):
        conn = FakeConnection(values=counts(
            active_checkpoints=1,
            active_checkpoint_revisions=2,
            ack_order_intents=1,
            active_checkpoints_with_matching_ack_intent=1,
            active_checkpoints_with_contiguous_revision_counts=1,
        ))
        result = await inspect_nonflat_ledger(conn)
        self.assertEqual(
            result["status"],
            "T100_CHECKPOINT_CANDIDATES_REQUIRE_EXCHANGE_RECONCILIATION",
        )
        self.assertFalse(result["t100_nonflat_recovery_verified"])
        self.assertFalse(result["exchange_read_performed"])

    async def test_unresolved_rows_block(self):
        for key in (
            "unresolved_order_intents", "unresolved_stop_amendments",
            "review_or_closing_checkpoints",
        ):
            conn = FakeConnection(values=counts(**{key: 1}))
            result = await inspect_nonflat_ledger(conn)
            self.assertEqual(result["status"], "LEDGER_UNRESOLVED_STATE_BLOCKED")

    async def test_orphan_ack_and_active_executor_lease_block(self):
        for extra, status in (
            ({"ack_order_intents": 1}, "LEDGER_UNMATCHED_ACK_INTENT_BLOCKED"),
            ({"active_executor_leases": 1},
             "UNEXPECTED_ACTIVE_LIVE_EXECUTOR_LEASE"),
        ):
            result = await inspect_nonflat_ledger(
                FakeConnection(values=counts(**extra))
            )
            self.assertEqual(result["status"], status)
            self.assertFalse(result["live_authorization"])

    async def test_missing_origin_ack_or_revision_blocks(self):
        for extra in (
            {"active_checkpoints": 1},
            {
                "active_checkpoints": 1,
                "ack_order_intents": 1,
                "active_checkpoints_with_matching_ack_intent": 1,
            },
        ):
            result = await inspect_nonflat_ledger(
                FakeConnection(values=counts(**extra))
            )
            self.assertEqual(
                result["status"], "LEDGER_RECOVERY_PROVENANCE_INCOMPLETE"
            )

    async def test_wrong_user_and_db_fail_before_reading_ledger(self):
        for opts, status in (
            ({"user": "mexc_scanner"}, "LEDGER_WRONG_RUNTIME_ROLE"),
            ({"database": "paper"}, "LEDGER_WRONG_DATABASE"),
            ({"readonly": "off"}, "LEDGER_SNAPSHOT_NOT_READ_ONLY"),
            ({"isolation": "read committed"}, "LEDGER_SNAPSHOT_NOT_READ_ONLY"),
        ):
            conn = FakeConnection(**opts)
            result = await inspect_nonflat_ledger(conn)
            self.assertEqual(result["status"], status)
            self.assertNotIn(COUNTS_SQL, conn.queries)
            self.assertFalse(result["t100_nonflat_recovery_verified"])

    async def test_driver_error_is_redacted(self):
        conn = FakeConnection(fail=True)
        result = await inspect_nonflat_ledger(conn)
        self.assertEqual(result["status"], "LEDGER_READ_FAILED")
        self.assertEqual(result["failure_type"], "RuntimeError")
        self.assertNotIn("DO_NOT_EVER_SHOW", str(result))
        self.assertFalse(result["live_authorization"])

    async def test_invalid_counts_fail_closed(self):
        for bad in (True, -1, 1.0, None):
            row = counts()
            row["active_checkpoints"] = bad
            result = await inspect_nonflat_ledger(
                FakeConnection(values=row)
            )
            self.assertEqual(result["status"], "LEDGER_COUNT_INVALID")
            self.assertFalse(result["t100_nonflat_recovery_verified"])


if __name__ == "__main__":
    unittest.main()
