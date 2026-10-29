"""Offline test coverage for read-only PostgreSQL runtime role preflight."""
import unittest

from app.t100_live_role_preflight import (
    LIVE_SEQUENCES,
    LIVE_TABLES,
    TARGET_ROLE,
    inspect_role_setup_preconditions,
)


class FakeConnection:
    def __init__(
        self, *, readonly="on", database="mexc_exhaustion",
        dbrole="mexc_scanner", create_role=True, target_exists=False,
        table_owner=None, sequence_owner=None, missing_table=False,
        missing_sequence=False, query_failure=False,
    ):
        self.readonly = readonly
        self.database = database
        self.dbrole = dbrole
        self.create_role = create_role
        self.target_exists = target_exists
        self.table_owner = table_owner or dbrole
        self.sequence_owner = sequence_owner or dbrole
        self.missing_table = missing_table
        self.missing_sequence = missing_sequence
        self.query_failure = query_failure
        self.executed = []

    async def fetchval(self, query, *args):
        self.executed.append(query)
        if self.query_failure:
            raise TimeoutError()
        if "transaction_read_only" in query:
            return self.readonly
        if "current_database()" in query:
            return self.database
        raise AssertionError(query)

    async def fetchrow(self, query, *args):
        self.executed.append(query)
        if "r.rolcreaterole" in query:
            return {
                "username": self.dbrole,
                "can_create_role": self.create_role,
                "is_superuser": False,
            }
        if "WHERE rolname=$1" in query:
            if self.target_exists:
                return {
                    "rolname": TARGET_ROLE,
                    "rolcanlogin": True,
                    "rolsuper": False,
                    "rolcreaterole": False,
                    "rolcreatedb": False,
                    "rolreplication": False,
                    "rolbypassrls": False,
                }
            return None
        raise AssertionError(query)

    async def fetch(self, query, *args):
        self.executed.append(query)
        if "c.relkind IN" in query:
            return [
                {"name": name, "owner_name": self.table_owner}
                for name in sorted(LIVE_TABLES)
                if not (self.missing_table and name == sorted(LIVE_TABLES)[0])
            ]
        if "c.relkind='S'" in query:
            return [
                {"name": name, "owner_name": self.sequence_owner}
                for name in sorted(LIVE_SEQUENCES)
                if not (self.missing_sequence and name == sorted(LIVE_SEQUENCES)[0])
            ]
        raise AssertionError(query)


class RolePreflightTests(unittest.IsolatedAsyncioTestCase):
    async def test_full_owner_and_create_privilege_pass(self):
        conn = FakeConnection()
        result = await inspect_role_setup_preconditions(conn)
        self.assertEqual(
            result["status"], "DIRECT_ROLE_CREATION_PREFLIGHT_PASSED"
        )
        self.assertTrue(result["live_table_owner_verified"])
        self.assertTrue(result["live_sequence_owner_verified"])
        self.assertTrue(result["runtime_role_absent"])
        self.assertFalse(result["live_authorization"])
        self.assertTrue(all(
            q.lstrip().startswith("SELECT") for q in conn.executed
        ))

    async def test_wrong_db_refused_before_catalog_queries(self):
        conn = FakeConnection(database="wrong")
        result = await inspect_role_setup_preconditions(conn)
        self.assertEqual(result["status"], "WRONG_DATABASE")
        self.assertEqual(len(conn.executed), 2)

    async def test_owner_cannot_create_role(self):
        result = await inspect_role_setup_preconditions(
            FakeConnection(create_role=False)
        )
        self.assertEqual(result["status"], "OWNER_LACKS_CREATE_ROLE")

    async def test_existing_runtime_role_requires_review(self):
        result = await inspect_role_setup_preconditions(
            FakeConnection(target_exists=True)
        )
        self.assertEqual(result["status"], "TARGET_ROLE_EXISTS_REVIEW_MANUALLY")

    async def test_missing_live_table_refused(self):
        result = await inspect_role_setup_preconditions(
            FakeConnection(missing_table=True)
        )
        self.assertEqual(result["status"], "LIVE_TABLE_OWNER_UNVERIFIED")

    async def test_mismatched_sequence_owner_refused(self):
        result = await inspect_role_setup_preconditions(
            FakeConnection(sequence_owner="other_owner")
        )
        self.assertEqual(result["status"], "LIVE_SEQUENCE_OWNER_UNVERIFIED")

    async def test_non_read_only_transaction_refused(self):
        result = await inspect_role_setup_preconditions(
            FakeConnection(readonly="off")
        )
        self.assertEqual(result["status"], "NOT_READ_ONLY")

    async def test_driver_error_redacted(self):
        result = await inspect_role_setup_preconditions(
            FakeConnection(query_failure=True)
        )
        self.assertEqual(result["status"], "ROLE_PREFLIGHT_READ_FAILED")
        self.assertEqual(result["failure_type"], "TimeoutError")
        self.assertNotIn("postgresql://", str(result))


if __name__ == "__main__":
    unittest.main()
