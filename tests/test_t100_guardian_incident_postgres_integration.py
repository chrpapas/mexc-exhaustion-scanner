"""Real disposable PostgreSQL 17 integration for guardian incident outbox.

Only localhost CI PostgreSQL. No Render, MEXC, or Discord access.
Tests transactional outbox, sticky latch, singleton, and least privilege.
"""
from __future__ import annotations

import asyncio
import os
from pathlib import Path
import unittest

from app.t100_live_guardian_incident_outbox import GuardianIncidentOutbox
from app.t100_live_guardian_durable_worker import (
    verify_alert_writer_role, GuardianDurableHalted,
)

LOCAL_DSN="postgresql://postgres:postgres@127.0.0.1:5432/postgres"
SCOPE="d"*64
ROOT=Path(__file__).resolve().parents[1]


async def connect():
    import asyncpg
    return await asyncpg.connect(LOCAL_DSN,timeout=5,command_timeout=8)


class GuardianIncidentPostgresIntegration(unittest.IsolatedAsyncioTestCase):
    @classmethod
    def setUpClass(cls):
        if os.environ.get("T100_RENDER_TEST_LOCAL_POSTGRES")!="YES":
            raise unittest.SkipTest("This test NEVER uses a production database")
        async def init():
            conn=await connect()
            try:
                # Necessary referents for privilege inspection. All are
                # disposable CI fixtures; no real T100 tables are modified.
                for name in (
                    "t100_live_position_checkpoints",
                    "t100_live_order_intents",
                    "t100_live_stop_amendments",
                    "t100_owned_emergency_close_intents",
                    "t100_guardian_account_enrollments",
                ):
                    await conn.execute(
                        f"CREATE TABLE IF NOT EXISTS public.{name} (ci_dummy int)"
                    )
                await conn.execute(
                    (ROOT/"migrations/035_t100_guardian_incident_outbox.sql")
                    .read_text()
                )
                await conn.execute(
                    "CREATE ROLE t100_ci_guardian_incident_writer NOLOGIN NOINHERIT"
                )
                await conn.execute(
                    "GRANT USAGE ON SCHEMA public TO t100_ci_guardian_incident_writer"
                )
                await conn.execute(
                    """GRANT SELECT,INSERT,UPDATE
                       ON public.t100_guardian_incident_state,
                          public.t100_guardian_incident_outbox
                       TO t100_ci_guardian_incident_writer"""
                )
                await conn.execute(
                    """GRANT USAGE ON SEQUENCE
                       public.t100_guardian_incident_outbox_seq_seq
                       TO t100_ci_guardian_incident_writer"""
                )
            finally:
                await conn.close()
        asyncio.run(init())

    async def asyncSetUp(self):
        self.conn=await connect()

    async def asyncTearDown(self):
        await self.conn.close()

    async def test_actual_restricted_writer_role_and_sticky_latch(self):
        await self.conn.execute("SET ROLE t100_ci_guardian_incident_writer")
        try:
            await verify_alert_writer_role(self.conn)
            outbox=GuardianIncidentOutbox(self.conn,scope_sha256=SCOPE)
            first=await outbox.observe(
                status="OBSERVER_UNENROLLED",alert=True)
            self.assertEqual(first.revision,1)
            self.assertTrue(first.critical_latched)
            pending=await outbox.pending()
            self.assertEqual(len(pending),1)
            self.assertEqual(pending[0].code,"OBSERVER_UNENROLLED")
            self.assertEqual(pending[0].attempts,0)
            self.assertEqual(await outbox.mark_attempt(pending[0].seq),1)
            self.assertEqual(await outbox.pending_count(),1)
            await outbox.mark_delivered(pending[0].seq)
            self.assertEqual(await outbox.pending_count(),0)

            second=await outbox.observe(status="OBSERVER_CYCLE_OK",alert=False)
            self.assertEqual(second.revision,2)
            self.assertTrue(second.critical_latched)
            newer=await outbox.pending()
            self.assertEqual(len(newer),1)
            self.assertEqual(newer[0].severity,"warning")
            again=await outbox.observe(status="OBSERVER_CYCLE_OK",alert=False)
            self.assertTrue(again.critical_latched)
            self.assertEqual(len(await outbox.pending()),1)
        finally:
            await self.conn.execute("RESET ROLE")

    async def test_cross_connection_restart_persists_unacked_incident(self):
        scope="e"*64
        await self.conn.execute("SET ROLE t100_ci_guardian_incident_writer")
        try:
            ledger=GuardianIncidentOutbox(self.conn,scope_sha256=scope)
            await ledger.observe(status="OBSERVER_SAFETY_ALERT",alert=True)
            pending=await ledger.pending()
            self.assertEqual(len(pending),1)
            await ledger.mark_attempt(pending[0].seq)
        finally:
            await self.conn.execute("RESET ROLE")
        other=await connect()
        try:
            await other.execute("SET ROLE t100_ci_guardian_incident_writer")
            led=GuardianIncidentOutbox(other,scope_sha256=scope)
            q=await led.pending()
            self.assertEqual(len(q),1)
            self.assertEqual(q[0].attempts,1)
            await led.mark_attempt(q[0].seq)
            self.assertEqual((await led.pending())[0].attempts,2)
            await led.mark_delivered(q[0].seq)
            self.assertEqual(await led.pending_count(),0)
        finally:
            await other.close()

    async def test_deny_owner_delete_or_latch_clear_and_outbox_identity_update(self):
        import asyncpg
        with self.assertRaises(asyncpg.PostgresError):
            await self.conn.execute(
                """UPDATE public.t100_guardian_incident_state
                   SET critical_latched=false, revision=revision+1
                   WHERE scope_sha256=$1""",SCOPE)
        with self.assertRaises(asyncpg.PostgresError):
            await self.conn.execute(
                "DELETE FROM public.t100_guardian_incident_state WHERE scope_sha256=$1",
                SCOPE)
        with self.assertRaises(asyncpg.PostgresError):
            await self.conn.execute(
                """UPDATE public.t100_guardian_incident_outbox
                   SET code='OBSERVER_OTHER'
                   WHERE scope_sha256=$1""",SCOPE)
        with self.assertRaises(asyncpg.PostgresError):
            await self.conn.execute(
                """DELETE FROM public.t100_guardian_incident_outbox
                   WHERE scope_sha256=$1""",SCOPE)

    async def test_writer_cannot_modify_trade_or_enrollment_tables(self):
        import asyncpg
        await self.conn.execute("SET ROLE t100_ci_guardian_incident_writer")
        try:
            for target in (
                "t100_live_position_checkpoints",
                "t100_live_order_intents",
                "t100_live_stop_amendments",
                "t100_owned_emergency_close_intents",
                "t100_guardian_account_enrollments",
            ):
                with self.subTest(table=target):
                    with self.assertRaises(asyncpg.PostgresError):
                        await self.conn.execute(
                            f"INSERT INTO public.{target} DEFAULT VALUES"
                        )
        finally:
            await self.conn.execute("RESET ROLE")

    async def test_writer_with_extra_trade_privilege_is_denied(self):
        await self.conn.execute(
            """GRANT UPDATE ON public.t100_live_position_checkpoints
               TO t100_ci_guardian_incident_writer"""
        )
        try:
            await self.conn.execute("SET ROLE t100_ci_guardian_incident_writer")
            try:
                with self.assertRaises(GuardianDurableHalted):
                    await verify_alert_writer_role(self.conn)
            finally:
                await self.conn.execute("RESET ROLE")
        finally:
            await self.conn.execute(
                """REVOKE UPDATE ON public.t100_live_position_checkpoints
                   FROM t100_ci_guardian_incident_writer"""
            )

    async def test_advisory_singleton_prevents_duplicate_worker(self):
        import asyncpg
        first=GuardianIncidentOutbox(self.conn,scope_sha256=SCOPE)
        await first.acquire_singleton()
        other=await connect()
        try:
            second=GuardianIncidentOutbox(other,scope_sha256=SCOPE)
            from app.t100_live_guardian_incident_outbox import GuardianIncidentHalted
            with self.assertRaises(GuardianIncidentHalted):
                await second.acquire_singleton()
        finally:
            await other.close()


if __name__=="__main__":
    unittest.main()
