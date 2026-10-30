"""REAL PostgreSQL 16 integration of V40 protected-entry atomic persistence.

Runs ONLY in an isolated GitHub Actions ephemeral service database using the
explicit CI-only opt-in and hardcoded loopback account. No MEXC, Render, POST,
production DB URLs, user secrets, exchange order writes or live authorization.

Tests 027-031 against PostgreSQL's actual transactions, row locks, unique
constraints and rollback; this does not certify exchange atomicity, native
stop execution, cross-margin risk or production role grants.
"""
from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from pathlib import Path
import os
import unittest

import asyncpg

from app.t100_live_checkpoint_journal import PostgresCheckpointJournal
from app.t100_live_db_inspect import EXPECTED_DATABASE
from app.t100_live_durable_recovery import (
    checkpoint_from_row, recover_nonflat,
)
from app.t100_live_four_slot_ledger_reader import (
    CHECKPOINTS_SQL, REVISIONS_SQL, INTENTS_SQL, AMENDMENTS_SQL,
)
from app.t100_live_v40_protected_commit import (
    PostgresProtectedV40Commit, ProtectedCommitHalted,
)
from tests.test_t100_live_four_slot_snapshot_coordinator import fixture
from tests.test_t100_live_v40_protected_commit import ProtectedCommitTests


MIGRATIONS = (
    "027_t100_live_order_intents.sql",
    "028_t100_live_vol30_h4.sql",
    "029_t100_live_durable_state.sql",
    "030_t100_live_executor_lease.sql",
    "031_t100_live_v40_four_slot_reservations.sql",
)
ROOT = Path(__file__).resolve().parents[1]
TEST_PASSWORD = "t100-ci-only-local-postgres"


def require_isolated_ci() -> None:
    safe = (
        os.environ.get("CI") == "true"
        and os.environ.get("T100_PG_INTEGRATION_CI_ONLY") == "yes"
        and os.environ.get("T100_EXECUTION_MODE") == "paper"
        and os.environ.get("T100_LIVE_ORDER_API_ENABLED") == "false"
        and os.environ.get("T100_LIVE_KILL_SWITCH") == "true"
        and os.environ.get("PGHOST") == "127.0.0.1"
        and os.environ.get("PGPORT") == "5432"
        and os.environ.get("PGDATABASE") == EXPECTED_DATABASE
        and os.environ.get("PGUSER") == "postgres"
        and os.environ.get("PGPASSWORD") == TEST_PASSWORD
        and not os.environ.get("DATABASE_URL")
        and not os.environ.get("MEXC_API_KEY")
        and not os.environ.get("MEXC_API_SECRET")
    )
    if not safe:
        raise RuntimeError("V40_EPHEMERAL_LOCALHOST_POSTGRES_CI_GUARD_FAILED")


class RealPostgresProtectedV40Tests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        require_isolated_ci()
        self.pool = await asyncpg.create_pool(
            host="127.0.0.1", port=5432,
            user="postgres", password=TEST_PASSWORD,
            database=EXPECTED_DATABASE, min_size=1, max_size=4,
            timeout=5,
        )
        async with self.pool.acquire() as db:
            if await db.fetchval("SELECT current_database()") != EXPECTED_DATABASE:
                raise RuntimeError("V40_PG_INCORRECT_DATABASE")
            if await db.fetchval(
                "SELECT to_regclass('public.t100_live_v40_slot_reservations')"
            ) is None:
                for migration in MIGRATIONS:
                    # These CREATE-ONLY migrations run only on the isolated
                    # ephemeral CI Postgres service, never the Render DB.
                    await db.execute((ROOT / "migrations" / migration).read_text())
            await db.execute("""
                TRUNCATE TABLE
                  public.t100_live_v40_slot_reservations,
                  public.t100_live_position_checkpoints,
                  public.t100_live_position_checkpoint_revisions,
                  public.t100_live_order_intents,
                  public.t100_live_order_fill_sets,
                  public.t100_live_order_fills,
                  public.t100_live_executor_leases
                RESTART IDENTITY CASCADE
            """)

        fixture_test = ProtectedCommitTests()
        await fixture_test.asyncSetUp()
        self.batch = fixture_test.batch
        self.witness = fixture_test.prior.before
        self.position_rows = fixture_test.position_rows
        self.stop_rows = fixture_test.stop_rows
        self.new_order = fixture_test.order
        self.observed_at = datetime.now(timezone.utc)
        self.lease = fixture_test.prior.pg
        self.executor = PostgresProtectedV40Commit(self.pool)

        # Simulate three ALREADY-owned and protected positions in the
        # real database. Their identities match the separately simulated
        # venue GET fixture; do not fake entry POSTs.
        previous, _, _, _ = fixture(3)
        async with self.pool.acquire() as db:
            for row in previous.checkpoints:
                cp = checkpoint_from_row(row)
                await db.execute("""
                    INSERT INTO public.t100_live_order_intents
                    (signal_id,symbol,external_oid,reference_price,
                     notional_usdt,volume_contracts,stop_price,state,
                     exchange_order_id)
                    VALUES($1,$2,$3,$4,$5,$6,$7,'ACK',$8)
                """,
                    700 + int(cp.position_id), cp.symbol, cp.external_oid,
                    cp.entry_price, cp.entry_price * cp.contracts,
                    cp.contracts, cp.stop_price, cp.exchange_order_id,
                )
            await db.execute("""
                INSERT INTO public.t100_live_executor_leases
                (lease_name,holder_id,fencing_token,lease_expires_at)
                VALUES ('t100-live-executor','worker-A',11,
                        clock_timestamp()+interval '5 minutes')
            """)
            entry = self.batch.entries[0]
            i = entry.intent
            await db.execute("""
                INSERT INTO public.t100_live_order_intents
                (signal_id,symbol,external_oid,reference_price,
                 notional_usdt,volume_contracts,stop_price,state)
                VALUES($1,$2,$3,$4,$5,$6,$7,'SUBMITTING')
            """,
                i.signal_id, i.symbol, i.external_oid, i.reference_price,
                i.notional_usdt, i.volume_contracts, i.stop_price,
            )
            await db.execute("""
                INSERT INTO public.t100_live_v40_slot_reservations
                (signal_id,symbol,external_oid,modeled_notional_usdt,
                 ledger_sha256,inventory_sha256,snapshot_observed_at,
                 holder_id,fencing_token,state)
                VALUES($1,$2,$3,$4,$5,$6,$7,'worker-A',11,'SUBMITTING')
            """,
                i.signal_id, i.symbol, i.external_oid,
                i.notional_usdt,
                self.witness.ledger_hash,
                self.witness.critical_inventory_hash,
                self.observed_at,
            )
        journal = PostgresCheckpointJournal(self.pool)
        for cp_row in previous.checkpoints:
            await journal.create_initial(checkpoint_from_row(cp_row))

    async def asyncTearDown(self):
        await self.pool.close()

    async def finalize(self):
        from app.t100_live_leader_fencing import LeaderLease
        return await self.executor.persist(
            batch=self.batch, pre_attempt_witness=self.witness,
            lease=LeaderLease("t100-live-executor", "worker-A", 11, 45),
            observed_at=self.observed_at,
        )

    async def state(self):
        async with self.pool.acquire() as db:
            queries = {
                "new_intent": """
                    SELECT state FROM public.t100_live_order_intents
                    WHERE external_oid=$1
                """,
                "slot": """
                    SELECT state FROM public.t100_live_v40_slot_reservations
                    WHERE signal_id=$1
                """,
            }
            entry = self.batch.entries[0]
            return {
                "new_intent": await db.fetchval(
                    queries["new_intent"], entry.intent.external_oid,
                ),
                "slot": await db.fetchval(
                    queries["slot"], entry.intent.signal_id,
                ),
                "fills": await db.fetchval(
                    "SELECT count(*) FROM public.t100_live_order_fills",
                ),
                "fill_sets": await db.fetchval(
                    "SELECT count(*) FROM public.t100_live_order_fill_sets",
                ),
                "checkpoints": await db.fetchval(
                    "SELECT count(*) FROM public.t100_live_position_checkpoints",
                ),
                "revisions": await db.fetchval(
                    "SELECT count(*) FROM public.t100_live_position_checkpoint_revisions",
                ),
            }

    async def test_real_postgres_atomic_commit_and_restart_recovery(self):
        before = await self.state()
        self.assertEqual(before, {
            "new_intent": "SUBMITTING", "slot": "SUBMITTING",
            "fills": 0, "fill_sets": 0, "checkpoints": 3, "revisions": 3,
        })
        result = await self.finalize()
        self.assertTrue(result.db_atomic_commit_observed)
        self.assertFalse(result.live_authorization)
        self.assertFalse(result.mexc_orders_sent)
        self.assertEqual(await self.state(), {
            "new_intent": "ACK", "slot": "PROTECTED",
            "fills": 1, "fill_sets": 1, "checkpoints": 4, "revisions": 4,
        })
        # Read ACTUAL persisted checkpoint/revision/intent rows (not a
        # handcrafted stand-in) and require the normal durable restart gate
        # to recover all four positions against simulated venue inventory.
        async with self.pool.acquire() as db:
            cp = [dict(row) for row in await db.fetch(CHECKPOINTS_SQL)]
            rev = [dict(row) for row in await db.fetch(REVISIONS_SQL)]
            intents = [dict(row) for row in await db.fetch(INTENTS_SQL)]
            amendments = [dict(row) for row in await db.fetch(AMENDMENTS_SQL)]
        self.assertEqual(len(cp), 4)
        _, old_orders, _, _ = fixture(3)
        recovered = recover_nonflat(
            checkpoint_rows=cp,
            revision_rows=rev,
            intent_rows=intents,
            stop_amendment_rows=amendments,
            orders=[*old_orders, self.new_order],
            positions=self.position_rows,
            stops=self.stop_rows,
            positions_complete=True, stops_complete=True,
        )
        self.assertEqual(len(recovered.recovered), 4)
        self.assertFalse(recovered.live_authorization)

    async def test_trigger_failure_rolls_back_ack_fills_checkpoint_and_slot(self):
        async with self.pool.acquire() as db:
            await db.execute("""
                CREATE FUNCTION public.t100_ci_block_protected()
                RETURNS trigger LANGUAGE plpgsql AS $$
                BEGIN
                  IF NEW.state = 'PROTECTED' THEN
                    RAISE EXCEPTION 'ci forced failure';
                  END IF;
                  RETURN NEW;
                END
                $$
            """)
            await db.execute("""
                CREATE TRIGGER t100_ci_protected_abort
                BEFORE UPDATE ON public.t100_live_v40_slot_reservations
                FOR EACH ROW EXECUTE FUNCTION public.t100_ci_block_protected()
            """)
        before = await self.state()
        try:
            with self.assertRaises(ProtectedCommitHalted):
                await self.finalize()
            self.assertEqual(await self.state(), before)
        finally:
            async with self.pool.acquire() as db:
                await db.execute("""
                    DROP TRIGGER t100_ci_protected_abort
                    ON public.t100_live_v40_slot_reservations
                """)
                await db.execute("DROP FUNCTION public.t100_ci_block_protected()")
        self.assertTrue((await self.finalize()).db_atomic_commit_observed)

    async def test_duplicate_after_ack_fails_closed_with_no_duplicate_fills(self):
        await self.finalize()
        before = await self.state()
        with self.assertRaises(ProtectedCommitHalted):
            await self.finalize()
        self.assertEqual(await self.state(), before)

    async def test_stale_leader_fence_blocks_without_mutation(self):
        async with self.pool.acquire() as db:
            await db.execute("""
                UPDATE public.t100_live_executor_leases
                SET fencing_token=12 WHERE lease_name='t100-live-executor'
            """)
        before = await self.state()
        with self.assertRaisesRegex(ProtectedCommitHalted, "LEASE_INVALID"):
            await self.finalize()
        self.assertEqual(await self.state(), before)

    async def test_concurrent_finalize_calls_do_not_duplicate_the_entry(self):
        attempted = await asyncio.gather(
            self.finalize(), self.finalize(), return_exceptions=True,
        )
        self.assertEqual(sum(not isinstance(x, Exception) for x in attempted), 1)
        self.assertEqual(await self.state(), {
            "new_intent": "ACK", "slot": "PROTECTED",
            "fills": 1, "fill_sets": 1, "checkpoints": 4, "revisions": 4,
        })


if __name__ == "__main__":
    require_isolated_ci()
    unittest.main()
