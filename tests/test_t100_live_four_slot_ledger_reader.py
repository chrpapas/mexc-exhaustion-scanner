"""Offline SQL-read-only contract tests; no PostgreSQL connection."""
from __future__ import annotations

import unittest

from app.t100_live_db_inspect import EXPECTED_DATABASE
from app.t100_live_role_preflight import TARGET_ROLE
from app.t100_live_four_slot_ledger_reader import (
    FourSlotLedgerReadHalted, PostgresFourSlotLedgerReader,
)


class Tx:
    def __init__(self, owner, isolation, readonly):
        self.owner = owner
        self.isolation = isolation
        self.readonly = readonly

    async def __aenter__(self):
        self.owner.transaction_active = True
        return self

    async def __aexit__(self, *args):
        self.owner.transaction_active = False


class Pool:
    def __init__(self):
        self.queries = []
        self.transaction_active = False
        self.readonly = "on"
        self.isolation = "repeatable read"
        self.database = EXPECTED_DATABASE
        self.user = TARGET_ROLE
        self.fail_fetch = False

    def acquire(self):
        return self

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return False

    def transaction(self, *, isolation, readonly):
        self.queries.append(("transaction", isolation, readonly))
        return Tx(self, isolation, readonly)

    async def fetchval(self, query):
        if not self.transaction_active:
            raise AssertionError("SELECT without a transaction")
        self.queries.append(("scalar", query))
        if "transaction_read_only" in query:
            return self.readonly
        if "transaction_isolation" in query:
            return self.isolation
        if "current_database()" in query:
            return self.database
        if "current_user" in query:
            return self.user
        raise AssertionError("unexpected scalar query")

    async def fetch(self, query):
        if not self.transaction_active:
            raise AssertionError("SELECT without a transaction")
        self.queries.append(("rows", query))
        if self.fail_fetch:
            raise RuntimeError("simulated outage")
        return []


class LedgerReaderTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.pool = Pool()
        self.reader = PostgresFourSlotLedgerReader(self.pool)

    async def test_all_four_selects_inside_verified_repeatable_read_transaction(self):
        snap = await self.reader.read_snapshot()
        self.assertTrue(snap.readonly_repeatable_read)
        self.assertEqual(snap.checkpoints, [])
        self.assertEqual(snap.revisions, [])
        self.assertEqual(snap.intents, [])
        self.assertEqual(snap.stop_amendments, [])
        self.assertEqual(
            self.pool.queries[0], ("transaction", "repeatable_read", True)
        )
        sqls = [q for typ, q, *rest in self.pool.queries if typ == "rows"]
        self.assertEqual(len(sqls), 4)
        for query in sqls:
            self.assertTrue(query.strip().upper().startswith("SELECT"))
            self.assertIn("public.t100_live_", query)
            self.assertNotIn("INSERT ", query.upper())
            self.assertNotIn("UPDATE ", query.upper())
            self.assertNotIn("DELETE ", query.upper())
        self.assertFalse(self.pool.transaction_active)

    async def test_wrong_database_blocks_before_data_fetch(self):
        self.pool.database = "wrong_db"
        with self.assertRaisesRegex(
            FourSlotLedgerReadHalted, "RESTRICTED_READONLY_CONTEXT_UNVERIFIED"
        ):
            await self.reader.read_snapshot()
        self.assertFalse(any(t == "rows" for t, *_ in self.pool.queries))

    async def test_wrong_role_blocks_before_data_fetch(self):
        self.pool.user = "mexc_scanner"
        with self.assertRaisesRegex(
            FourSlotLedgerReadHalted, "RESTRICTED_READONLY_CONTEXT_UNVERIFIED"
        ):
            await self.reader.read_snapshot()
        self.assertFalse(any(t == "rows" for t, *_ in self.pool.queries))

    async def test_nonreadonly_transaction_blocks(self):
        self.pool.readonly = "off"
        with self.assertRaisesRegex(
            FourSlotLedgerReadHalted, "RESTRICTED_READONLY_CONTEXT_UNVERIFIED"
        ):
            await self.reader.read_snapshot()

    async def test_unexpected_isolation_blocks(self):
        self.pool.isolation = "read committed"
        with self.assertRaisesRegex(
            FourSlotLedgerReadHalted, "RESTRICTED_READONLY_CONTEXT_UNVERIFIED"
        ):
            await self.reader.read_snapshot()

    async def test_failed_select_redacts_underlying_exception(self):
        self.pool.fail_fetch = True
        with self.assertRaisesRegex(
            FourSlotLedgerReadHalted, "FOUR_SLOT_LEDGER_READ_FAILED"
        ):
            await self.reader.read_snapshot()

    async def test_missing_pool_never_constructed(self):
        with self.assertRaisesRegex(ValueError, "POOL_MISSING"):
            PostgresFourSlotLedgerReader(None)


if __name__ == "__main__":
    unittest.main()
