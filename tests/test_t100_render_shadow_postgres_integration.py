"""Actual PostgreSQL integration tests: ONLY on isolated CI localhost.

Never use DATABASE_URL or Render credentials. Hard-coded loopback
Postgres 17 service plus an opt-in CI-only environment guard.
The tests run the deliberately non-idempotent migration 031 and check
real database trigger behavior, cross-connection advisory locks and
sentinel stale detection.
"""
from __future__ import annotations

import asyncio
import hashlib
import os
import unittest
from pathlib import Path

from app.t100_render_shadow_witness import (
    RenderShadowWitness, ShadowWitnessFailure,
)
from app.t100_render_shadow_sentinel import inspect_witness


LOCAL_DSN = "postgresql://postgres:postgres@127.0.0.1:5432/postgres"
MIGRATION = (
    Path(__file__).resolve().parents[1]
    / "migrations" / "031_t100_render_shadow_witness.sql"
)


async def _connect():
    import asyncpg
    return await asyncpg.connect(LOCAL_DSN, timeout=5, command_timeout=8)


class IsolatedPostgresWitnessTests(unittest.IsolatedAsyncioTestCase):
    @classmethod
    def setUpClass(cls):
        if os.getenv("T100_RENDER_TEST_LOCAL_POSTGRES") != "YES":
            raise unittest.SkipTest("Never access a production or external database")
        async def init():
            conn = await _connect()
            try:
                # Running the DDL here is safe ONLY because the DSN is
                # hardcoded to a disposable GitHub Actions localhost DB.
                await conn.execute(MIGRATION.read_text())
            finally:
                await conn.close()
        asyncio.run(init())

    async def asyncSetUp(self):
        self.conn = await _connect()
        self.scope = hashlib.sha256(self._testMethodName.encode()).hexdigest()

    async def asyncTearDown(self):
        await self.conn.close()

    async def test_healthy_kill_restart_and_database_triggers(self):
        writer = RenderShadowWitness(self.conn, scope_sha256=self.scope)
        await writer.acquire_singleton()
        first = await writer.record(
            run_id="a"*12,cycle_index=0,state="OBSERVING_NON_AUTHORIZING",
            kill_latched=False,kill_reason=None,
        )
        self.assertEqual(first.revision,1)
        self.assertFalse(first.kill_latched)
        latched = await writer.record(
            run_id="a"*12,cycle_index=1,state="OBSERVING_KILL_LATCHED",
            kill_latched=True,kill_reason="NATIVE_PROTECTION_LOST",
        )
        self.assertEqual(latched.revision,2)
        self.assertEqual(latched.kill_reason,"NATIVE_PROTECTION_LOST")
        self.assertTrue(latched.kill_latched)

        # A real PostgreSQL trigger must reject direct attempt to
        # remove/replace the historical kill record.
        import asyncpg
        with self.assertRaises(asyncpg.PostgresError):
            await self.conn.execute(
                "UPDATE public.t100_shadow_witness "
                "SET revision=revision+1,kill_latched=false,kill_reason=NULL "
                "WHERE scope_sha256=$1",self.scope,
            )

        restored = await writer.restore()
        self.assertTrue(restored.kill_latched)
        self.assertEqual(restored.kill_reason,"NATIVE_PROTECTION_LOST")
        self.assertGreaterEqual(restored.age_seconds,0.0)

        # A second process after the first connection closes can attach
        # but still cannot reset the kill via an unlatched observation.
        await self.conn.close()
        self.conn = await _connect()
        writer2 = RenderShadowWitness(self.conn, scope_sha256=self.scope)
        await writer2.acquire_singleton()
        restarted = await writer2.record(
            run_id="b"*12,cycle_index=0,state="OBSERVING_NON_AUTHORIZING",
            kill_latched=False,kill_reason=None,
        )
        self.assertTrue(restarted.kill_latched)
        self.assertEqual(restarted.kill_reason,"NATIVE_PROTECTION_LOST")
        self.assertEqual(restarted.state,"OBSERVING_KILL_LATCHED")
        self.assertEqual(restarted.revision,3)

    async def test_actual_postgres_session_lock_fences_duplicate_writer(self):
        first = RenderShadowWitness(self.conn,scope_sha256=self.scope)
        await first.acquire_singleton()
        other_conn = await _connect()
        try:
            second = RenderShadowWitness(other_conn,scope_sha256=self.scope)
            with self.assertRaisesRegex(
                ShadowWitnessFailure,"SHADOW_WITNESS_WRITER_ALREADY_RUNNING"
            ):
                await second.acquire_singleton()
            # Only the initial process has the right to publish while
            # its advisory session lock is held.
            record = await first.record(
                run_id="c"*12,cycle_index=0,state="OBSERVING_NON_AUTHORIZING",
                kill_latched=False,kill_reason=None,
            )
            self.assertEqual(record.revision,1)
            await self.conn.close()
            self.conn=await _connect()
            # Once session closes the second can acquire the lock.
            await second.acquire_singleton()
        finally:
            await other_conn.close()

    async def test_sentinel_reads_fresh_and_stale_from_real_db_clock(self):
        writer = RenderShadowWitness(self.conn,scope_sha256=self.scope)
        await writer.acquire_singleton()
        await writer.record(
            run_id="d"*12,cycle_index=0,state="OBSERVING_NON_AUTHORIZING",
            kill_latched=False,kill_reason=None,
        )
        fresh = await inspect_witness(self.conn,self.scope,stale_seconds=240)
        self.assertEqual(fresh.status,"SHADOW_WITNESS_RECENT_NONAUTHORIZING")
        self.assertFalse(fresh.alert)

        # Advance a row's DB timestamp positively only for this case?
        # A new isolated scope can be inserted with an old timestamp.
        stale_scope = hashlib.sha256((self.scope+"stale").encode()).hexdigest()
        await self.conn.execute(
            "INSERT INTO public.t100_shadow_witness "
            "(scope_sha256,revision,run_id,cycle_index,observed_at,state,"
            "kill_latched,kill_reason) "
            "VALUES($1,1,$2,0,clock_timestamp()-interval '600 seconds',"
            "'OBSERVING_NON_AUTHORIZING',false,NULL)",
            stale_scope,"e"*12,
        )
        stale = await inspect_witness(self.conn,stale_scope,stale_seconds=240)
        self.assertEqual(stale.status,"SHADOW_WITNESS_STALE")
        self.assertTrue(stale.alert)

    async def test_database_rejects_any_true_trade_authorization(self):
        import asyncpg
        with self.assertRaises(asyncpg.PostgresError):
            await self.conn.execute(
                "INSERT INTO public.t100_shadow_witness "
                "(scope_sha256,revision,run_id,cycle_index,state,"
                "kill_latched,kill_reason,entry_allowed,live_authorization) "
                "VALUES($1,1,$2,0,'OBSERVING_NON_AUTHORIZING',"
                "false,NULL,true,true)",
                self.scope,"f"*12,
            )


if __name__=="__main__":
    unittest.main()
