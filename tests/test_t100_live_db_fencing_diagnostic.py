"""Offline stateful test of the two-connection PostgreSQL fencing diagnostic.

The fake models the SQL predicates against a shared logical database clock.
The production operator command uses real asyncpg, not this fake.
"""
import ssl
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

from app.t100_live_db_fencing_diagnostic import (
    FencingDiagnosticHalted,
    LEASE_PREFIX,
    CONFIRMATION,
    _main,
    exercise_test_fencing,
)


class SharedDB:
    def __init__(self):
        self.now = 0.
        self.rows = {}
        self.writes = []

    async def tick(self, seconds):
        self.now += seconds


class Conn:
    def __init__(self, db):
        self.db = db

    async def fetchrow(self, query, *args):
        if "INSERT INTO t100_live_executor_leases" in query:
            name, holder, ttl = args
            record = self.db.rows.get(name)
            if record is None:
                record = dict(holder=holder, token=1, expires=self.db.now + ttl)
            elif record["holder"] != holder and record["expires"] > self.db.now:
                return None
            elif record["holder"] != holder or record["expires"] <= self.db.now:
                record = dict(
                    holder=holder, token=record["token"] + 1,
                    expires=self.db.now + ttl,
                )
            else:
                record["expires"] = self.db.now + ttl
            self.db.rows[name] = record
            self.db.writes.append((name, "ACQUIRE"))
            return {"fencing_token": record["token"]}
        if "SELECT lease_expires_at <= now()" in query:
            record = self.db.rows.get(args[0])
            return {"expired": record["expires"] <= self.db.now} if record else None
        raise AssertionError(query)

    async def fetchval(self, query, *args):
        if "SELECT count(*)" in query:
            return int(args[0] in self.db.rows)
        if "RETURNING fencing_token" in query and "UPDATE" in query:
            name, holder, token = args[:3]
            record = self.db.rows.get(name)
            if record is None or record["holder"] != holder or record["token"] != token:
                return None
            if "AND lease_expires_at > clock_timestamp()" in query:
                if record["expires"] <= self.db.now:
                    return None
                record["expires"] = self.db.now + args[3]
                self.db.writes.append((name, "RENEW"))
            else:
                record["expires"] = self.db.now
                self.db.writes.append((name, "RELEASE"))
            return token
        if "SELECT 1" in query:
            name, holder, token = args
            record = self.db.rows.get(name)
            return 1 if (
                record and record["holder"] == holder
                and record["token"] == token
                and record["expires"] > self.db.now
            ) else None
        raise AssertionError(query)

    async def execute(self, query, *args):
        if "SET lease_expires_at=now()-interval" in query:
            record = self.db.rows.get(args[0])
            if record:
                record["expires"] = self.db.now - 1.
                self.db.writes.append((args[0], "EXPIRE_TEST_LEASE"))
            return
        raise AssertionError(query)


class FencingDBDiagnosticTests(unittest.IsolatedAsyncioTestCase):
    async def test_two_independent_connections_simulated_sql_handoff(self):
        db = SharedDB()
        name = LEASE_PREFIX + "testnonce"
        result = await exercise_test_fencing(
            Conn(db), Conn(db), lease_name=name, sleep=db.tick,
        )
        self.assertEqual(
            result["status"], "RESTRICTED_POSTGRES_FENCING_DIAGNOSTIC_PASSED"
        )
        for key in (
            "simultaneous_second_holder_denied",
            "real_ttl_takeover_verified",
            "stale_old_holder_rejected",
            "same_holder_expired_reacquire_verified",
            "same_holder_stale_token_rejected",
            "released_lease_recovered",
            "final_test_lease_expired",
        ):
            self.assertIs(result[key], True, key)
        self.assertFalse(result["live_authorization"])
        self.assertFalse(result["real_executor_lease_accessed"])
        self.assertFalse(result["mexc_orders_sent"])
        self.assertEqual(set(db.rows), {name})
        self.assertLess(db.rows[name]["expires"], db.now)
        self.assertEqual(db.rows[name]["token"], 4)

    async def test_real_connection_uses_verified_tls_before_any_test_lease(self):
        """Blocked runtime audit must stop before lease writes; TLS checks both connections."""
        first = AsyncMock()
        second = AsyncMock()
        first.fetchval.side_effect = [
            "mexc_exhaustion", "t100_live_executor_runtime",
        ]
        second.fetchval.side_effect = [
            "mexc_exhaustion", "t100_live_executor_runtime",
        ]
        connect = AsyncMock(side_effect=[first, second])
        with (
            patch("builtins.input", return_value=CONFIRMATION),
            patch("getpass.getpass", side_effect=[
                "test-password", "postgresql://owner:unused@example.com/mexc_exhaustion",
            ]),
            patch.dict(
                "sys.modules",
                {"asyncpg": Mock(connect=connect)},
            ),
            patch(
                "app.t100_live_db_fencing_diagnostic.independent_runtime_audit",
                new=AsyncMock(return_value={"status": "NOT_CERTIFIED"}),
            ),
        ):
            with self.assertRaisesRegex(
                FencingDiagnosticHalted,
                "RUNTIME_ROLE_EFFECTIVE_PRIVILEGES_UNVERIFIED",
            ):
                await _main()
        self.assertEqual(connect.await_count, 2)
        for call in connect.await_args_list:
            context = call.kwargs["ssl"]
            self.assertIsInstance(context, ssl.SSLContext)
            self.assertTrue(context.check_hostname)
            self.assertEqual(context.verify_mode, ssl.CERT_REQUIRED)
            self.assertEqual(call.kwargs["user"], "t100_live_executor_runtime")
        first.execute.assert_not_awaited()
        second.execute.assert_not_awaited()
        first.close.assert_awaited_once()
        second.close.assert_awaited_once()

    async def test_keychain_lookup_uses_only_executor_identity(self):
        with (
            patch("app.t100_live_db_fencing_diagnostic.sys.platform", "darwin"),
            patch(
                "app.t100_live_db_fencing_diagnostic.subprocess.run",
                return_value=SimpleNamespace(
                    returncode=0, stdout="keychain-runtime-secret\\n",
                ),
            ) as run,
        ):
            from app.t100_live_db_fencing_diagnostic import (
                _runtime_password_from_mac_keychain,
            )
            self.assertEqual(
                _runtime_password_from_mac_keychain(),
                "keychain-runtime-secret",
            )
        command = run.call_args.args[0]
        self.assertEqual(command[0:2], (
            "/usr/bin/security", "find-generic-password",
        ))
        self.assertEqual(command[-1], "-w")
        self.assertIn("t100_live_executor_runtime", command)
        self.assertNotIn("t100_shadow_witness_writer", command)
        self.assertNotIn("t100_shadow_sentinel_outbox", command)

    async def test_keychain_lookup_missing_stops_before_database_or_orders(self):
        from app.t100_live_db_fencing_diagnostic import (
            _runtime_password_from_mac_keychain,
        )
        with (
            patch("app.t100_live_db_fencing_diagnostic.sys.platform", "darwin"),
            patch(
                "app.t100_live_db_fencing_diagnostic.subprocess.run",
                return_value=SimpleNamespace(returncode=44, stdout=""),
            ) as run,
        ):
            with self.assertRaisesRegex(
                FencingDiagnosticHalted,
                "MAC_KEYCHAIN_EXECUTOR_PASSWORD_NOT_FOUND",
            ):
                _runtime_password_from_mac_keychain()
        for call in run.call_args_list:
            command=call.args[0]
            self.assertNotIn("t100_shadow_witness_writer", command)
            self.assertNotIn("t100_shadow_sentinel_outbox", command)

    async def test_keychain_flag_blocks_before_db_if_executor_password_missing(self):
        from app.t100_live_db_fencing_diagnostic import _main
        with (
            patch("builtins.input", return_value=CONFIRMATION),
            patch(
                "app.t100_live_db_fencing_diagnostic._runtime_password_from_mac_keychain",
                side_effect=FencingDiagnosticHalted(
                    "MAC_KEYCHAIN_EXECUTOR_PASSWORD_NOT_FOUND"
                ),
            ),
            patch("getpass.getpass") as prompt,
        ):
            with self.assertRaisesRegex(
                FencingDiagnosticHalted,
                "MAC_KEYCHAIN_EXECUTOR_PASSWORD_NOT_FOUND",
            ):
                await _main(keychain=True)
        prompt.assert_not_called()

    async def test_rejects_production_lease_name_without_any_writes(self):
        db = SharedDB()
        with self.assertRaisesRegex(
            FencingDiagnosticHalted, "TEST_ONLY_LEASE_NAME_REQUIRED",
        ):
            await exercise_test_fencing(
                Conn(db), Conn(db),
                lease_name="t100-live-executor",
                sleep=db.tick,
            )
        self.assertEqual(db.writes, [])
        self.assertEqual(db.rows, {})

    async def test_rejects_test_name_collision_without_modifying_existing(self):
        db = SharedDB()
        name = LEASE_PREFIX + "existing"
        db.rows[name] = dict(holder="old", token=20, expires=99999.)
        with self.assertRaisesRegex(
            FencingDiagnosticHalted, "TEST_LEASE_NAME_COLLISION",
        ):
            await exercise_test_fencing(
                Conn(db), Conn(db), lease_name=name, sleep=db.tick,
            )
        self.assertEqual(db.rows[name]["token"], 20)
        # Emergency cleanup should never target an occupied test-name collision.
        self.assertEqual(db.rows[name]["holder"], "old")


if __name__ == "__main__":
    unittest.main()
