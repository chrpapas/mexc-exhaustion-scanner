"""Real PostgreSQL 17 test of migration 033 on isolated CI localhost ONLY.

Hard-coded loopback DSN; NEVER accepts production DB env or credentials.
"""
from __future__ import annotations

import asyncio
import os
from pathlib import Path
import unittest
from decimal import Decimal as D

from app.t100_live_owned_emergency_guardian import OwnedShort, close_intent_oid
from app.t100_live_owned_close_pg_ledger import PostgresOwnedCloseIntentLedger

LOCAL_DSN = "postgresql://postgres:postgres@127.0.0.1:5432/postgres"
DDL = Path(__file__).resolve().parents[1] / "migrations" / "033_t100_owned_emergency_close_intents.sql"


async def local_connection():
    import asyncpg
    return await asyncpg.connect(LOCAL_DSN, timeout=5, command_timeout=8)


class OwnedClosePostgresIntegration(unittest.IsolatedAsyncioTestCase):
    @classmethod
    def setUpClass(cls):
        if os.getenv("T100_RENDER_TEST_LOCAL_POSTGRES") != "YES":
            raise unittest.SkipTest("Refusing non-CI or external database")
        async def init():
            conn = await local_connection()
            try:
                await conn.execute(DDL.read_text())
            finally:
                await conn.close()
        asyncio.run(init())

    async def asyncSetUp(self):
        self.conn = await local_connection()
        self.owned = OwnedShort(
            scope_sha256="c"*64, symbol="BTC_USDT",
            position_id=str(100 + hash(self._testMethodName) % 1000000),
            entry_external_oid="t100v4_abcdefgh1234_entry",
            entry_order_id="112", contracts=D("1"),
            stop_order_id="113", stop_price=D("74000"),
            checkpoint_version=1,
        )
        self.oid = close_intent_oid(self.owned)

    async def asyncTearDown(self):
        await self.conn.close()

    async def test_db_persists_one_close_claim_across_connections_and_no_reset(self):
        ledger = PostgresOwnedCloseIntentLedger(self.conn)
        self.assertTrue(await ledger.prepare_if_absent(self.oid, self.owned))
        other = await local_connection()
        try:
            secondary = PostgresOwnedCloseIntentLedger(other)
            self.assertFalse(await secondary.prepare_if_absent(self.oid, self.owned))
            self.assertEqual(await ledger.read_state(self.oid,self.owned),"PREPARED")
            self.assertTrue(await ledger.transition(self.oid,"PREPARED","SUBMITTING"))
            self.assertEqual(await ledger.read_state(self.oid,self.owned),"SUBMITTING")
            self.assertFalse(await secondary.transition(self.oid,"PREPARED","SUBMITTING"))
            self.assertTrue(await secondary.transition(self.oid,"SUBMITTING","UNKNOWN"))
            self.assertEqual(await secondary.read_state(self.oid,self.owned),"UNKNOWN")
            self.assertFalse(await ledger.transition(self.oid,"PREPARED","SUBMITTING"))
            # Even privileged direct UPDATE cannot reset a consumed intent.
            import asyncpg
            with self.assertRaises(asyncpg.PostgresError):
                await other.execute(
                    "UPDATE public.t100_owned_emergency_close_intents "
                    "SET state='PREPARED' WHERE external_oid=$1", self.oid
                )
            with self.assertRaises(asyncpg.PostgresError):
                await other.execute(
                    "DELETE FROM public.t100_owned_emergency_close_intents "
                    "WHERE external_oid=$1", self.oid
                )
            with self.assertRaises(asyncpg.PostgresError):
                await other.execute(
                    "UPDATE public.t100_owned_emergency_close_intents "
                    "SET position_id='999999999' WHERE external_oid=$1",self.oid
                )
            self.assertTrue(await ledger.transition(self.oid,"UNKNOWN","VERIFIED"))
            self.assertEqual(await ledger.read_state(self.oid,self.owned),"VERIFIED")
            self.assertFalse(await secondary.transition(self.oid,"UNKNOWN","VERIFIED"))
            state = await other.fetchval(
                "SELECT state FROM public.t100_owned_emergency_close_intents "
                "WHERE external_oid=$1", self.oid,
            )
            self.assertEqual(state, "VERIFIED")
        finally:
            await other.close()

    async def test_read_state_requires_exact_durable_ownership(self):
        ledger = PostgresOwnedCloseIntentLedger(self.conn)
        self.assertTrue(await ledger.prepare_if_absent(self.oid,self.owned))
        from dataclasses import replace
        from app.t100_live_owned_emergency_guardian import OwnedGuardianHalted
        # Same position, wrong entry order ID must not read unrelated journal.
        with self.assertRaises(OwnedGuardianHalted):
            await ledger.read_state(
                self.oid,replace(self.owned,entry_order_id="999"))
        with self.assertRaises(OwnedGuardianHalted):
            await ledger.read_state("t100gc_"+"f"*22,self.owned)

    async def test_unique_scope_position_prevents_distinct_oid_second_close(self):
        ledger = PostgresOwnedCloseIntentLedger(self.conn)
        self.assertTrue(await ledger.prepare_if_absent(self.oid,self.owned))
        # Different entry order id would otherwise produce a distinct OID.
        from dataclasses import replace
        other_owned = replace(self.owned, entry_order_id="555")
        other_oid = close_intent_oid(other_owned)
        self.assertNotEqual(other_oid, self.oid)
        self.assertFalse(await ledger.prepare_if_absent(other_oid,other_owned))

    async def test_restricted_role_denied_trading_table_and_delete(self):
        await self.conn.execute(
            "CREATE ROLE t100_ci_owned_close NOLOGIN NOINHERIT"
        )
        await self.conn.execute(
            "CREATE TABLE public.t100_ci_owned_trade_secret(dummy int)"
        )
        await self.conn.execute(
            "GRANT USAGE ON SCHEMA public TO t100_ci_owned_close"
        )
        await self.conn.execute(
            "GRANT SELECT,INSERT,UPDATE ON public.t100_owned_emergency_close_intents "
            "TO t100_ci_owned_close"
        )
        import asyncpg
        await self.conn.execute("SET ROLE t100_ci_owned_close")
        try:
            ledger = PostgresOwnedCloseIntentLedger(self.conn)
            self.assertTrue(await ledger.prepare_if_absent(self.oid,self.owned))
            self.assertTrue(await ledger.transition(self.oid,"PREPARED","SUBMITTING"))
            with self.assertRaises(asyncpg.PostgresError):
                await self.conn.fetchval(
                    "SELECT dummy FROM public.t100_ci_owned_trade_secret"
                )
            with self.assertRaises(asyncpg.PostgresError):
                await self.conn.execute(
                    "DELETE FROM public.t100_owned_emergency_close_intents "
                    "WHERE external_oid=$1",self.oid
                )
        finally:
            await self.conn.execute("RESET ROLE")


if __name__ == "__main__":
    unittest.main()
