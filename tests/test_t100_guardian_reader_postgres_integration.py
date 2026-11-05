"""Actual PostgreSQL 17 SELECT-only role and pinned checkpoint observer tests.

Hard-coded disposable CI localhost. No Render/database credentials. No
external exchange access or order placement.
"""
from __future__ import annotations

import asyncio
from decimal import Decimal as D
import hashlib
import os
import unittest

from app.t100_live_guardian_checkpoint_reader import (
    GuardianCheckpointHalted, load_pinned_owned_checkpoint,
)

LOCAL_DSN = "postgresql://postgres:postgres@127.0.0.1:5432/postgres"
KEY = "ci-guardian-scoped-api-key-1234"
SCOPE = hashlib.sha256(KEY.encode()).hexdigest()
OID = "t100v4_abcdef012345_entry"

SCHEMA = """
CREATE TABLE public.t100_live_position_checkpoints (
 position_id text PRIMARY KEY,
 symbol text NOT NULL, external_oid text NOT NULL,
 exchange_order_id text NOT NULL, contracts numeric NOT NULL,
 entry_price numeric NOT NULL, stop_order_id text NOT NULL,
 stop_price numeric NOT NULL, best_profit_pct numeric NOT NULL,
 armed boolean NOT NULL, lifecycle_state text NOT NULL,
 checkpoint_version integer NOT NULL
);
CREATE TABLE public.t100_live_position_checkpoint_revisions (
 position_id text NOT NULL, checkpoint_version integer NOT NULL,
 write_reason text NOT NULL, symbol text NOT NULL,
 external_oid text NOT NULL, exchange_order_id text NOT NULL,
 contracts numeric NOT NULL, entry_price numeric NOT NULL,
 stop_order_id text NOT NULL, stop_price numeric NOT NULL,
 best_profit_pct numeric NOT NULL, armed boolean NOT NULL,
 lifecycle_state text NOT NULL
);
CREATE TABLE public.t100_live_order_intents (
 symbol text NOT NULL, external_oid text NOT NULL,
 exchange_order_id text NOT NULL, volume_contracts numeric NOT NULL,
 stop_price numeric NOT NULL, state text NOT NULL
);
CREATE TABLE public.t100_live_stop_amendments (
 external_id text, symbol text, position_id text, stop_order_id text,
 verified_stop_order_id text, previous_stop numeric,
 proposed_stop numeric, state text
);
INSERT INTO public.t100_live_position_checkpoints VALUES (
 '555','BTC_USDT','t100v4_abcdef012345_entry','666',1,73000,
 '777',74500,0,false,'OPEN',1
);
INSERT INTO public.t100_live_position_checkpoint_revisions VALUES (
 '555',1,'ENTRY_PROTECTED','BTC_USDT','t100v4_abcdef012345_entry',
 '666',1,73000,'777',74500,0,false,'OPEN'
);
INSERT INTO public.t100_live_order_intents VALUES (
 'BTC_USDT','t100v4_abcdef012345_entry','666',1,74500,'ACK'
);
CREATE ROLE t100_ci_guardian_reader NOLOGIN NOINHERIT;
CREATE ROLE t100_ci_guardian_writer NOLOGIN NOINHERIT;
GRANT USAGE ON SCHEMA public TO
 t100_ci_guardian_reader,t100_ci_guardian_writer;
GRANT SELECT ON public.t100_live_position_checkpoints,
 public.t100_live_position_checkpoint_revisions,
 public.t100_live_order_intents,
 public.t100_live_stop_amendments
 TO t100_ci_guardian_reader,t100_ci_guardian_writer;
GRANT INSERT ON public.t100_live_position_checkpoints
 TO t100_ci_guardian_writer;
"""


async def connect():
    import asyncpg
    return await asyncpg.connect(LOCAL_DSN,timeout=5,command_timeout=8)


async def read(conn):
    return await load_pinned_owned_checkpoint(
        conn, api_key=KEY,pinned_scope_sha256=SCOPE,
        pinned_position_id="555",
    )


class GuardianReaderPostgresIntegration(unittest.IsolatedAsyncioTestCase):
    @classmethod
    def setUpClass(cls):
        if os.environ.get("T100_RENDER_TEST_LOCAL_POSTGRES") != "YES":
            raise unittest.SkipTest("No access to an external database")
        async def create():
            conn=await connect()
            try:
                await conn.execute(SCHEMA)
            finally:
                await conn.close()
        asyncio.run(create())

    async def asyncSetUp(self):
        self.conn=await connect()

    async def asyncTearDown(self):
        await self.conn.close()

    async def test_actual_select_only_role_with_readonly_transaction(self):
        await self.conn.execute("SET ROLE t100_ci_guardian_reader")
        try:
            async with self.conn.transaction(readonly=True):
                result=await read(self.conn)
                self.assertEqual(
                    result.status,
                    "DURABLE_OWNED_CHECKPOINT_PROVENANCE_VERIFIED_READ_ONLY",
                )
                self.assertEqual(result.owned.entry_order_id,"666")
                self.assertTrue(result.checkpoint_revisions_validated)
                self.assertFalse(result.live_authorization)
        finally:
            await self.conn.execute("RESET ROLE")

    async def test_writer_role_must_be_refused_even_inside_readonly_tx(self):
        await self.conn.execute("SET ROLE t100_ci_guardian_writer")
        try:
            async with self.conn.transaction(readonly=True):
                with self.assertRaisesRegex(
                    GuardianCheckpointHalted,"DB_ROLE_HAS_MUTATION_PRIVILEGES"
                ):
                    await read(self.conn)
        finally:
            await self.conn.execute("RESET ROLE")

    async def test_select_only_role_requires_readonly_transaction(self):
        await self.conn.execute("SET ROLE t100_ci_guardian_reader")
        try:
            async with self.conn.transaction(readonly=False):
                with self.assertRaisesRegex(
                    GuardianCheckpointHalted,"DB_ROLE_HAS_MUTATION_PRIVILEGES"
                ):
                    await read(self.conn)
        finally:
            await self.conn.execute("RESET ROLE")

    async def test_manual_or_other_position_pin_rejected(self):
        await self.conn.execute("SET ROLE t100_ci_guardian_reader")
        try:
            async with self.conn.transaction(readonly=True):
                with self.assertRaisesRegex(
                    GuardianCheckpointHalted,"PINNED_POSITION_ID_NOT_IN_DB"
                ):
                    await load_pinned_owned_checkpoint(
                        self.conn,api_key=KEY,pinned_scope_sha256=SCOPE,
                        pinned_position_id="999"
                    )
        finally:
            await self.conn.execute("RESET ROLE")


if __name__=="__main__":
    unittest.main()
