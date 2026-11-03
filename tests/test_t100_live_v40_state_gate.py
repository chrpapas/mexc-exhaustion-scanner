"""Offline fail-closed V40 durable dispatch state gate regressions.

Uses only mocked SELECT responses. No credentials, network, SQL writes,
production databases, or exchange mutations.
"""
from __future__ import annotations

import unittest

from app.t100_live_v40_state_gate import assess_v40_unresolved_dispatch_state


COUNTS = dict(
    unresolved_reservations=0,
    unresolved_dispatch_claims=0,
    unclaimed_protected_reservations=0,
    dispatch_claim_provenance_mismatches=0,
    protected_reservations=1,
    total_dispatch_claims=1,
)


class RecordLike:
    """Minimal mapping behavior of asyncpg.Record (not a dict)."""
    def __init__(self, data):
        self.data = data
    def __getitem__(self, key):
        return self.data[key]


class ReadPool:
    def __init__(self):
        self.schema = RecordLike(dict(slots_present=True, claims_present=True))
        self.counts = RecordLike(dict(COUNTS))
        self.calls = []
        self.fail = False

    async def fetchrow(self, sql):
        self.calls.append(sql)
        if self.fail:
            raise ConnectionError("private database URL must never leak")
        if "to_regclass(" in sql:
            return self.schema
        if "unclaimed_protected_reservations" in sql:
            return self.counts
        raise AssertionError("unexpected SQL")


class V40StateGateTests(unittest.IsolatedAsyncioTestCase):
    async def test_recordlike_clean_is_explicitly_nonauthorizing(self):
        p = ReadPool()
        r = await assess_v40_unresolved_dispatch_state(p)
        self.assertEqual(r["status"], "V40_DB_STATE_CLEAR_NONAUTHORIZING")
        self.assertTrue(r["v40_unresolved_dispatch_state_absent"])
        self.assertEqual(r["total_dispatch_claims"], 1)
        self.assertEqual(r["protected_reservations"], 1)
        self.assertEqual(len(p.calls), 2)
        self.assertFalse(r["entry_allowed"])
        self.assertFalse(r["live_authorization"])
        self.assertFalse(r["mexc_orders_sent"])

    async def test_migration_missing_prevents_count_query(self):
        for field in ("slots_present", "claims_present"):
            with self.subTest(field=field):
                p = ReadPool()
                p.schema = RecordLike({
                    **dict(slots_present=True, claims_present=True),
                    field: False,
                })
                r = await assess_v40_unresolved_dispatch_state(p)
                self.assertEqual(
                    r["status"], "V40_DB_STATE_MIGRATIONS_NOT_OBSERVED",
                )
                self.assertEqual(len(p.calls), 1)
                self.assertFalse(r["v40_unresolved_dispatch_state_absent"])

    async def test_all_four_unresolved_v40_types_block(self):
        for field in (
            "unresolved_reservations",
            "unresolved_dispatch_claims",
            "unclaimed_protected_reservations",
            "dispatch_claim_provenance_mismatches",
        ):
            with self.subTest(field=field):
                p = ReadPool()
                rows = dict(COUNTS)
                rows[field] = 1
                p.counts = RecordLike(rows)
                r = await assess_v40_unresolved_dispatch_state(p)
                self.assertEqual(
                    r["status"], "V40_DB_DISPATCH_OR_RESERVATION_UNRESOLVED",
                )
                self.assertFalse(r["v40_unresolved_dispatch_state_absent"])
                self.assertFalse(r["live_authorization"])

    async def test_invalid_counts_fail_closed(self):
        for bad in (None, "0", True, -1, 0.0):
            with self.subTest(bad=bad):
                p = ReadPool()
                p.counts = RecordLike({
                    **COUNTS, "unresolved_dispatch_claims": bad,
                })
                r = await assess_v40_unresolved_dispatch_state(p)
                self.assertEqual(r["status"], "V40_DB_STATE_COUNTS_INVALID")
                self.assertFalse(r["v40_unresolved_dispatch_state_absent"])

    async def test_missing_counts_and_database_error_fail_closed(self):
        p = ReadPool()
        p.counts = None
        r = await assess_v40_unresolved_dispatch_state(p)
        self.assertEqual(r["status"], "V40_DB_STATE_COUNTS_MISSING")
        p = ReadPool()
        p.fail = True
        r = await assess_v40_unresolved_dispatch_state(p)
        self.assertEqual(r["status"], "V40_DB_STATE_READ_FAILED")
        self.assertNotIn("private database URL", str(r))
        self.assertFalse(r["entry_allowed"])
        self.assertFalse(r["live_authorization"])

    async def test_missing_pool_fails_closed_without_exception(self):
        r = await assess_v40_unresolved_dispatch_state(None)
        self.assertEqual(r["status"], "V40_DB_STATE_POOL_MISSING")
        self.assertFalse(r["v40_unresolved_dispatch_state_absent"])


if __name__ == "__main__":
    unittest.main()
