"""Read-only V40 claim privilege audit regression tests (no DB writes)."""
from __future__ import annotations

import unittest

from app.t100_live_v40_dispatch_privilege_audit import (
    assess_v40_dispatch_claim_privileges,
    CLAIM_PRIVILEGES_SQL,
)


def rights(**overrides):
    data = {
        "table_owner": "postgres",
        "can_select": True,
        "can_insert": True,
        "can_update": False,
        "can_delete": False,
        "can_truncate": False,
        "can_trigger": False,
        "can_create_schema_objects": False,
        "can_create_database_objects": False,
        "immutable_trigger_enabled": True,
    }
    data.update(overrides)
    return data


class Connection:
    def __init__(self):
        self.readonly = "on"
        self.isolation = "repeatable read"
        self.database = "mexc_exhaustion"
        self.role = "t100_live_executor_runtime"
        self.row = rights()
        self.requests = []
        self.fail = False

    async def fetchval(self, sql):
        self.requests.append(sql)
        if self.fail:
            raise RuntimeError("sensitive database host")
        if "transaction_read_only" in sql:
            return self.readonly
        if "transaction_isolation" in sql:
            return self.isolation
        if "current_database()" in sql:
            return self.database
        if "current_user" in sql:
            return self.role
        raise AssertionError(sql)

    async def fetchrow(self, sql):
        self.requests.append(sql)
        return self.row


class AuditTests(unittest.IsolatedAsyncioTestCase):
    async def test_correct_restricted_role_and_immutable_trigger(self):
        conn = Connection()
        result = await assess_v40_dispatch_claim_privileges(conn)
        self.assertEqual(
            result["status"],
            "V40_DISPATCH_CLAIM_PRIVILEGES_VERIFIED_NONAUTHORIZING",
        )
        self.assertTrue(result["dispatch_claim_privileges_verified"])
        self.assertTrue(result["immutable_trigger_verified"])
        self.assertTrue(result["runtime_ddl_rights_absent"])
        self.assertFalse(result["entry_allowed"])
        self.assertFalse(result["live_authorization"])
        self.assertFalse(result["mexc_orders_sent"])
        self.assertEqual(conn.requests[-1], CLAIM_PRIVILEGES_SQL)

    async def test_disallowed_role_context_never_reads_claim_catalog(self):
        for attr, bad in (
            ("readonly", "off"), ("isolation", "read committed"),
            ("database", "other"), ("role", "postgres"),
        ):
            with self.subTest(attr=attr):
                conn = Connection()
                setattr(conn, attr, bad)
                result = await assess_v40_dispatch_claim_privileges(conn)
                self.assertEqual(
                    result["status"],
                    "V40_DISPATCH_RESTRICTED_CONTEXT_UNVERIFIED",
                )
                self.assertFalse(result["dispatch_claim_privileges_verified"])
                self.assertNotIn(CLAIM_PRIVILEGES_SQL, conn.requests)

    async def test_every_forbidden_ddl_or_claim_right_blocks(self):
        for field, value in (
            ("table_owner", "t100_live_executor_runtime"),
            ("can_select", False),
            ("can_insert", False),
            ("can_update", True),
            ("can_delete", True),
            ("can_truncate", True),
            ("can_trigger", True),
            ("can_create_schema_objects", True),
            ("can_create_database_objects", True),
            ("immutable_trigger_enabled", False),
        ):
            with self.subTest(field=field):
                conn = Connection()
                conn.row = rights(**{field: value})
                result = await assess_v40_dispatch_claim_privileges(conn)
                self.assertEqual(
                    result["status"], "V40_DISPATCH_CLAIM_PRIVILEGES_BLOCKED",
                )
                self.assertFalse(result["dispatch_claim_privileges_verified"])
                self.assertFalse(result["live_authorization"])

    async def test_missing_table_invalid_shape_and_failure_fail_closed(self):
        for row in (None, rights(can_delete=None), rights(table_owner=None)):
            with self.subTest(row=row):
                conn = Connection()
                conn.row = row
                result = await assess_v40_dispatch_claim_privileges(conn)
                self.assertFalse(result["dispatch_claim_privileges_verified"])
                self.assertFalse(result["entry_allowed"])
        conn = Connection()
        conn.fail = True
        result = await assess_v40_dispatch_claim_privileges(conn)
        self.assertEqual(
            result["status"], "V40_DISPATCH_PRIVILEGE_AUDIT_READ_FAILED",
        )
        self.assertNotIn("sensitive database host", str(result))


if __name__ == "__main__":
    unittest.main()
