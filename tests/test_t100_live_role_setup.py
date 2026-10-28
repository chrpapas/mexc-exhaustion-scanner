"""Offline fail-closed tests for SQL-created T100 restricted DB login."""
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

from app.t100_live_role_setup import (
    PINNED_GRANTS_GIT_BLOB,
    PRIVILEGES_PATH,
    ROLE_CONFIRM,
    TARGET_ROLE,
    RuntimeRoleSetupBlocked,
    _password_literal,
    independent_runtime_audit,
    pinned_grants,
    provision_role,
)


class FakeTx:
    def __init__(self, conn, readonly=False):
        self.conn = conn
        self.readonly = readonly

    async def __aenter__(self):
        self.conn.transactions.append(("START", self.readonly))
        return self

    async def __aexit__(self, exc_type, exc, tb):
        self.conn.transactions.append(
            ("ROLLBACK" if exc_type is not None else "COMMIT", self.readonly)
        )
        return False


class FakeOwner:
    def __init__(self, *, preflight_status="DIRECT_ROLE_CREATION_PREFLIGHT_PASSED",
                 wrong_database=False, lock=True, role_exists=False,
                 invalid_attributes=False):
        self.preflight_status = preflight_status
        self.wrong_database = wrong_database
        self.lock = lock
        self.role_exists = role_exists
        self.invalid_attributes = invalid_attributes
        self.statements = []
        self.transactions = []

    def transaction(self, *, readonly=False):
        return FakeTx(self, readonly)

    async def execute(self, stmt, *args):
        self.statements.append(stmt)

    async def fetchval(self, sql, *args):
        if "current_database()" in sql:
            return "bad" if self.wrong_database else "mexc_exhaustion"
        if "pg_try_advisory_xact_lock" in sql:
            return self.lock
        if "SELECT EXISTS" in sql:
            return self.role_exists
        raise AssertionError(sql)

    async def fetchrow(self, sql, *args):
        if "rolcanlogin" in sql:
            return dict(
                rolcanlogin=True,
                rolinherit=self.invalid_attributes,
                rolsuper=False,
                rolcreatedb=False,
                rolcreaterole=False,
                rolreplication=False,
                rolbypassrls=False,
            )
        raise AssertionError(sql)


class FakeRuntime:
    def __init__(self, *, actual_role=TARGET_ROLE, create_priv=False,
                 prod_priv=False, locked_schema=False):
        self.actual_role = actual_role
        self.create_priv = create_priv
        self.prod_priv = prod_priv
        self.locked_schema = locked_schema
        self.transactions = []

    def transaction(self, *, readonly=False):
        return FakeTx(self, readonly)

    async def fetchval(self, sql, *args):
        if "current_user" in sql:
            return self.actual_role
        raise AssertionError(sql)

    async def fetchrow(self, sql, *args):
        if "pg_catalog.pg_roles" in sql:
            return dict(
                rolcanlogin=True, rolinherit=False, rolsuper=False,
                rolcreatedb=False, rolcreaterole=False,
                rolreplication=False, rolbypassrls=False,
            )
        if "has_schema_privilege" in sql:
            return {
                "can_create_schema_objects": self.create_priv,
                "can_create_in_database": self.create_priv,
            }
        raise AssertionError(sql)

    async def fetch(self, sql, *args):
        if "pg_catalog.pg_class" in sql:
            return [{
                "table_name": "t100_positions",
                "can_select": self.prod_priv,
                "can_insert": False,
                "can_update": False,
                "can_delete": False,
                "can_truncate": False,
            }]
        raise AssertionError(sql)


class RoleSetupTests(unittest.IsolatedAsyncioTestCase):
    def test_real_template_is_pinned_and_only_fixed_role_substituted(self):
        text = pinned_grants()
        self.assertNotIn("__T100_LIVE_RUNTIME_ROLE__", text)
        self.assertIn("GRANT", text)
        self.assertIn(TARGET_ROLE, text)
        self.assertEqual(len(PINNED_GRANTS_GIT_BLOB), 40)

    def test_tampered_grants_blocked(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            path = root / PRIVILEGES_PATH
            path.parent.mkdir(parents=True)
            path.write_text("GRANT ALL PRIVILEGES ON ALL TABLES TO public;")
            with self.assertRaisesRegex(
                RuntimeRoleSetupBlocked, "TEMPLATE_HASH_MISMATCH"
            ):
                pinned_grants(root)

    def test_password_requires_strong_not_visible_in_results(self):
        with self.assertRaisesRegex(
            RuntimeRoleSetupBlocked, "PASSWORD_LENGTH"
        ):
            _password_literal("weak")
        self.assertEqual(
            _password_literal("ab'cd" * 8),
            "'ab''cd" * 7 + "ab''cd'",
        )

    async def test_creation_is_transactional_and_has_no_live_switch(self):
        conn = FakeOwner()
        with patch(
            "app.t100_live_role_setup._read_preflight",
            new_callable=AsyncMock,
            return_value={"status": conn.preflight_status},
        ):
            result = await provision_role(
                conn, password="R" * 45, grants="GRANT ...",
            )
        self.assertEqual(
            result["status"],
            "RUNTIME_ROLE_CREATED_PENDING_INDEPENDENT_LOGIN_AUDIT",
        )
        self.assertFalse(result["live_authorization"])
        self.assertTrue(result["grants_applied"])
        self.assertNotIn("R" * 45, str(result))
        self.assertEqual(conn.transactions[-1], ("COMMIT", False))
        self.assertTrue(any(
            "NOINHERIT" in stmt and "NOCREATEROLE" in stmt
            for stmt in conn.statements
        ))
        self.assertIn("SET LOCAL search_path = public", conn.statements)

    async def test_broken_preflight_blocks_without_write_transaction(self):
        conn = FakeOwner(preflight_status="OWNER_LACKS_CREATE_ROLE")
        with patch(
            "app.t100_live_role_setup._read_preflight",
            new_callable=AsyncMock,
            return_value={"status": conn.preflight_status},
        ):
            with self.assertRaisesRegex(
                RuntimeRoleSetupBlocked, "ROLE_PREFLIGHT_BLOCKED"
            ):
                await provision_role(
                    conn, password="x" * 40, grants="GRANT ...",
                )
        self.assertEqual(conn.statements, [])

    async def test_lock_and_role_collision_block_without_create(self):
        for options in ({"lock": False}, {"role_exists": True},
                        {"wrong_database": True}):
            conn = FakeOwner(**options)
            with patch(
                "app.t100_live_role_setup._read_preflight",
                new_callable=AsyncMock,
                return_value={
                    "status": "DIRECT_ROLE_CREATION_PREFLIGHT_PASSED"
                },
            ):
                with self.assertRaises(RuntimeRoleSetupBlocked):
                    await provision_role(
                        conn, password="x" * 40, grants="GRANT ...",
                    )
            self.assertEqual(conn.transactions[-1], ("ROLLBACK", False))
            self.assertFalse(any(
                "CREATE ROLE" in stmt for stmt in conn.statements
            ))

    async def test_role_attribute_failure_rolls_back(self):
        conn = FakeOwner(invalid_attributes=True)
        with patch(
            "app.t100_live_role_setup._read_preflight",
            new_callable=AsyncMock,
            return_value={"status": "DIRECT_ROLE_CREATION_PREFLIGHT_PASSED"},
        ):
            with self.assertRaisesRegex(
                RuntimeRoleSetupBlocked, "ATTRIBUTES_INVALID"
            ):
                await provision_role(
                    conn, password="x" * 40, grants="GRANT ...",
                )
        self.assertEqual(conn.transactions[-1], ("ROLLBACK", False))

    async def test_independent_login_checks_actual_role_and_production_acl(self):
        with patch(
            "app.t100_live_role_setup.inspect_live_database",
            new_callable=AsyncMock,
            return_value={
                "status": "LIVE_DATABASE_READ_ONLY_PRECHECK_PASSED",
                "unresolved_mutations_absent": True,
            },
        ):
            good = await independent_runtime_audit(FakeRuntime())
            self.assertEqual(
                good["status"], "RESTRICTED_T100_RUNTIME_ROLE_VERIFIED"
            )
            self.assertEqual(good["production_tables_inspected"], 1)
            self.assertFalse(good["live_authorization"])
            bad = await independent_runtime_audit(
                FakeRuntime(prod_priv=True)
            )
            self.assertEqual(
                bad["status"],
                "RESTRICTED_T100_RUNTIME_ROLE_VERIFICATION_FAILED",
            )
            self.assertEqual(bad["production_tables_with_privileges"], 1)
            bad_create = await independent_runtime_audit(
                FakeRuntime(create_priv=True)
            )
            self.assertFalse(
                bad_create["runtime_create_privileges_absent"]
            )
            bad_identity = await independent_runtime_audit(
                FakeRuntime(actual_role="mexc_scanner")
            )
            self.assertEqual(
                bad_identity["status"], "CONNECTED_ROLE_IDENTITY_MISMATCH"
            )


if __name__ == "__main__":
    unittest.main()
