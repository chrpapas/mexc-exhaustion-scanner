"""Offline combined dynamic readiness bundle tests."""
import unittest

from app.t100_live_readiness_bundle import run_dynamic_readiness_bundle
from app.t100_live_schema_gate import (
    LIVE_TABLE_COLUMNS, MUTABLE_TABLES, REQUIRED_INDEXES,
)


class Pool:
    def __init__(self):
        self.tables = tuple(sorted(LIVE_TABLE_COLUMNS))
        self.columns = {
            table: set(cols) for table, cols in LIVE_TABLE_COLUMNS.items()
        }
        self.indexes = set(REQUIRED_INDEXES)
        self.unresolved = {
            "prepared_order_intents": 0,
            "submitting_order_intents": 0,
            "unknown_order_intents": 0,
            "unresolved_stop_amendments": 0,
            "review_checkpoints": 0,
            "closing_checkpoints": 0,
        }

    async def fetch(self, sql, values):
        if "information_schema.tables" in sql:
            return [{"table_name": x} for x in self.tables]
        if "information_schema.columns" in sql:
            return [
                {"table_name": t, "column_name": c}
                for t, cols in self.columns.items()
                for c in cols
            ]
        if "pg_indexes" in sql:
            return [{"indexname": x} for x in self.indexes]
        if "has_table_privilege" in sql:
            return [{
                "table_name": table,
                "can_select": True,
                "can_insert": True,
                "can_update": table in MUTABLE_TABLES,
                "can_delete": False,
                "can_truncate": False,
            } for table in self.tables]
        raise AssertionError("unexpected fetch")

    async def fetchrow(self, sql):
        return dict(self.unresolved)


class Client:
    def __init__(self):
        self.positions = []
        self.stops = []
        self.assets = [{
            "currency": "USDT",
            "positionMargin": 0,
            "unrealized": 0,
        }]
        self.counts = {
            "sumCount": 0,
            "limitOrderCount": 0,
            "stopOrderCount": 0,
            "planOrderCount": 0,
            "trackOrderCount": 0,
        }

    async def _private_get(self, path, params=None):
        if path == "/api/v1/private/position/open_positions":
            return [dict(x) for x in self.positions]
        if path == "/api/v1/private/stoporder/open_orders":
            return [dict(x) for x in self.stops]
        if path == "/api/v1/private/account/assets":
            return [dict(x) for x in self.assets]
        if path == "/api/v1/private/order/open_order_total_count":
            return dict(self.counts)
        raise AssertionError(path)


class DynamicReadinessTests(unittest.IsolatedAsyncioTestCase):
    async def test_flat_clean_account_can_pass_dynamic_bundle(self):
        r = await run_dynamic_readiness_bundle(
            client=Client(),
            pool=Pool(),
        )
        self.assertTrue(r["dynamic_readiness_verified"])
        self.assertEqual(r["dynamic_blockers"], [])
        self.assertFalse(r["live_authorization"])
        self.assertFalse(r["entry_allowed"])

    async def test_schema_failure_is_exposed_as_blocker(self):
        p = Pool()
        p.tables = p.tables[:-1]
        r = await run_dynamic_readiness_bundle(
            client=Client(),
            pool=p,
        )
        self.assertFalse(r["dynamic_readiness_verified"])
        self.assertIn(
            "durable_schema_deployed_verified",
            r["dynamic_blockers"],
        )

    async def test_unresolved_state_is_exposed_as_blocker(self):
        p = Pool()
        p.unresolved["unknown_order_intents"] = 1
        r = await run_dynamic_readiness_bundle(
            client=Client(),
            pool=p,
        )
        self.assertIn(
            "unresolved_mutations_absent",
            r["dynamic_blockers"],
        )

    async def test_nonflat_requires_durable_recovery(self):
        c = Client()
        c.positions = [{
            "positionId": "10",
            "symbol": "SIREN_USDT",
            "positionType": 2,
            "state": 1,
            "holdVol": 5,
            "im": 10,
            "unRealizedPnl": 1,
        }]
        c.stops = [{
            "id": "99",
            "positionId": "10",
            "symbol": "SIREN_USDT",
            "positionType": 2,
            "state": 1,
            "isFinished": 0,
            "stopLossPrice": 175,
            "vol": 5,
        }]
        c.assets = [{
            "currency": "USDT",
            "positionMargin": 10,
            "unrealized": 1,
        }]
        c.counts["sumCount"] = 1
        c.counts["stopOrderCount"] = 1

        blocked = await run_dynamic_readiness_bundle(
            client=c,
            pool=Pool(),
            durable_nonflat_recovery_verified=False,
        )
        self.assertFalse(blocked["account_reconciled"])

        recovered = await run_dynamic_readiness_bundle(
            client=c,
            pool=Pool(),
            durable_nonflat_recovery_verified=True,
        )
        self.assertTrue(recovered["account_reconciled"])
        self.assertTrue(recovered["dynamic_readiness_verified"])


if __name__ == "__main__":
    unittest.main()
