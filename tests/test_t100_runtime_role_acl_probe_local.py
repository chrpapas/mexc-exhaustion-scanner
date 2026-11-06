"""Offline regression coverage for the Mac's strictly read-only role ACL audit."""
from __future__ import annotations

import unittest

from tools.t100_runtime_role_acl_probe_local import (
    ALLOWED_STRATEGY_UPDATES,
    ROLE,
    SEQUENCES,
    STRATEGY_COLUMNS,
    TABLES,
    inspect_target_role_acl,
)


class _FakeConnection:
    def __init__(self, *, read_only=True, database="mexc_exhaustion",
                 role_present=True, override=None, drop_table=False):
        self.read_only = read_only
        self.database = database
        self.role_present = role_present
        self.override = override or {}
        self.drop_table = drop_table
        self.sql = []

    async def fetchval(self, sql, *args):
        self.sql.append(sql)
        if "transaction_read_only" in sql:
            return "on" if self.read_only else "off"
        if "current_database()" in sql:
            return self.database
        if "current_user" in sql:
            return "mexc_scanner"
        raise AssertionError(sql)

    async def fetchrow(self, sql, *args):
        self.sql.append(sql)
        if "FROM pg_catalog.pg_roles" in sql:
            if not self.role_present:
                return None
            return {
                "rolcanlogin": True, "rolsuper": False,
                "rolcreaterole": False, "rolcreatedb": False,
                "rolreplication": False, "rolbypassrls": False,
                **self.override.get("role", {}),
            }
        if "pg_has_role(" in sql:
            return {
                "owner_can_set_role": False,
                "can_create_in_public": False,
                "can_create_in_database": False,
                **self.override.get("role_grants", {}),
            }
        raise AssertionError(sql)

    async def fetch(self, sql, *args):
        self.sql.append(sql)
        self_role = args[0]
        assert self_role == ROLE
        if "unnest($2::text[],$3::text[])" in sql:
            return [
                {
                    "table_name": table, "column_name": column,
                    "can_update": column in ALLOWED_STRATEGY_UPDATES[table],
                    **self.override.get(("column", table, column), {}),
                }
                for table, column in STRATEGY_COLUMNS
            ]
        if "sequence_name" in sql:
            return [
                {
                    "sequence_name": name, "can_usage": True,
                    "can_select": True, "can_update": False,
                    **self.override.get(("sequence", name), {}),
                }
                for name in SEQUENCES
            ]
        if "table_name" in sql:
            names = TABLES[:-1] if self.drop_table else TABLES
            return [
                {
                    "table_name": name, "can_select": True,
                    "can_insert": True,
                    "can_update": name in {
                        "t100_live_order_intents",
                        "t100_live_position_checkpoints",
                        "t100_live_stop_amendments",
                    },
                    "can_delete": False, "can_truncate": False,
                    **self.override.get(("table", name), {}),
                }
                for name in names
            ]
        raise AssertionError(sql)


class TestTargetRoleAclProbe(unittest.IsolatedAsyncioTestCase):
    async def test_clean_role_is_only_non_authorizing_acl_match(self):
        conn = _FakeConnection()
        report = await inspect_target_role_acl(conn)
        self.assertEqual(report["status"], "TARGET_ROLE_ACL_MATCH_NONAUTHORIZING")
        self.assertEqual(report["mismatch_count"], 0)
        self.assertFalse(report["live_authorization"])
        self.assertFalse(report["current_session_is_target_role"])
        self.assertEqual(report["table_count"], len(TABLES))
        self.assertEqual(report["sequence_count"], len(SEQUENCES))
        self.assertTrue(all(
            "SELECT" in sql or "current_" in sql or "FROM" in sql
            for sql in conn.sql
        ))

    async def test_owner_login_not_evidence_of_runtime_privileges(self):
        role = next(iter(SEQUENCES))
        conn = _FakeConnection(override={("sequence", role): {"can_update": True}})
        report = await inspect_target_role_acl(conn)
        self.assertEqual(report["status"], "TARGET_ROLE_ACL_MISMATCH")
        self.assertIn(f"SEQUENCE:{role}:can_update:expected_false", report["mismatches"])

    async def test_unwanted_table_mutation_is_reported(self):
        name = TABLES[0]
        conn = _FakeConnection(override={("table", name): {"can_delete": True}})
        report = await inspect_target_role_acl(conn)
        self.assertIn(f"TABLE:{name}:can_delete:expected_false", report["mismatches"])

    async def test_schema_create_unsafe(self):
        conn = _FakeConnection(override={"role_grants": {
            "can_create_in_public": True,
        }})
        report = await inspect_target_role_acl(conn)
        self.assertIn("ROLE_UNSAFE_can_create_in_public", report["mismatches"])

    async def test_missing_evidence_fails_closed(self):
        report = await inspect_target_role_acl(_FakeConnection(drop_table=True))
        self.assertEqual(report["status"], "TARGET_ROLE_ACL_EVIDENCE_INCOMPLETE")

    async def test_unknown_role_fails_closed(self):
        report = await inspect_target_role_acl(_FakeConnection(role_present=False))
        self.assertEqual(report["status"], "TARGET_ROLE_NOT_FOUND")

    async def test_wrong_database_and_readwrite_fail_closed(self):
        wrong = await inspect_target_role_acl(_FakeConnection(database="other_db"))
        self.assertEqual(wrong["status"], "WRONG_DATABASE_IDENTITY")
        not_readonly = await inspect_target_role_acl(
            _FakeConnection(read_only=False)
        )
        self.assertEqual(not_readonly["status"], "NOT_IN_READ_ONLY_TRANSACTION")


if __name__ == "__main__":
    unittest.main()
