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
    "032_t100_live_v40_dispatch_claims.sql",
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
    async def test_lease_expiry_uses_wall_clock_inside_long_transaction(self):
        from app.t100_live_leader_fencing import PostgresLeaderLeaseStore
        async with self.pool.acquire() as db:
            async with db.transaction():
                await db.execute("""UPDATE t100_live_executor_leases
                    SET lease_expires_at=clock_timestamp()+interval '100 milliseconds'
                    WHERE lease_name='t100-live-executor'""")
                store = PostgresLeaderLeaseStore(db)
                await db.execute("SELECT pg_sleep(0.2)")
                self.assertFalse(await store.assert_current('t100-live-executor', 'worker-A', 11))
                self.assertFalse(await store.renew('t100-live-executor', 'worker-A', 11, 30))
                token, acquired = await store.acquire('t100-live-executor', 'worker-A', 30)
                self.assertTrue(acquired)
                self.assertEqual(token, 12)
                self.assertTrue(await store.assert_current('t100-live-executor', 'worker-A', 12))

    async def asyncSetUp(self):
        require_isolated_ci()
        self.read_pool = None
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
        if self.read_pool is not None:
            await self.read_pool.close()
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

    async def restricted_dispatch_claim_reader(self):
        """Disposable SELECT-only runtime account, never production credentials."""
        from app.t100_live_four_slot_dispatch_claim_reader import (
            PostgresFourSlotDispatchClaimReader,
        )
        from app.t100_live_role_preflight import TARGET_ROLE

        require_isolated_ci()
        async with self.pool.acquire() as db:
            await db.execute("""
                DO $claimroleci$
                BEGIN
                    IF NOT EXISTS (
                        SELECT 1 FROM pg_roles
                        WHERE rolname='t100_live_executor_runtime'
                    ) THEN
                        CREATE ROLE t100_live_executor_runtime
                        LOGIN PASSWORD 't100-ci-only-local-postgres'
                        NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION;
                    END IF;
                END $claimroleci$
            """)
            await db.execute(
                "GRANT CONNECT ON DATABASE mexc_exhaustion TO t100_live_executor_runtime"
            )
            await db.execute(
                "GRANT USAGE ON SCHEMA public TO t100_live_executor_runtime"
            )
            await db.execute("""
                GRANT SELECT ON
                    public.t100_live_v40_dispatch_claims,
                    public.t100_live_v40_slot_reservations,
                    public.t100_live_order_intents
                TO t100_live_executor_runtime
            """)
        self.read_pool = await asyncpg.create_pool(
            host="127.0.0.1", port=5432, user=TARGET_ROLE,
            password=TEST_PASSWORD, database=EXPECTED_DATABASE,
            min_size=1, max_size=1, timeout=5,
            server_settings={"default_transaction_read_only": "on"},
        )
        return PostgresFourSlotDispatchClaimReader(self.read_pool)

    async def test_restricted_reader_requires_exact_claim_after_restart(self):
        from app.t100_live_four_slot_dispatch_claims import (
            PostgresFourSlotDispatchClaims,
        )
        from app.t100_live_leader_fencing import LeaderLease

        stage = self.batch.entries[0].stage
        reader = await self.restricted_dispatch_claim_reader()
        absent = await reader.inspect([stage])
        self.assertEqual(absent.status, "DISPATCH_CLAIMS_MISSING_NO_RETRY")
        self.assertFalse(absent.all_claims_exact_and_durable)
        self.assertFalse(absent.resubmission_allowed)
        self.assertFalse(absent.live_authorization)

        await PostgresFourSlotDispatchClaims(self.pool).claim(
            stage=stage,
            lease=LeaderLease("t100-live-executor", "worker-A", 11, 45),
        )
        found = await reader.inspect([stage])
        self.assertEqual(
            found.status, "DISPATCH_CLAIMS_PRESENT_RECONCILE_ONLY"
        )
        self.assertTrue(found.all_claims_exact_and_durable)
        self.assertEqual(found.claimed_attempts, 1)
        self.assertFalse(found.resubmission_allowed)
        self.assertFalse(found.mexc_orders_sent)

        # Crash recovery may find a prior UNKNOWN or finalized ACK. Neither
        # state can justify another order POST for this immutable claim.
        for target_slot, target_intent in (
            ("UNKNOWN", "UNKNOWN"), ("ACK", "ACK"),
            ("PROTECTED", "ACK"),
        ):
            with self.subTest(target_slot=target_slot):
                async with self.pool.acquire() as db:
                    await db.execute("""
                        UPDATE public.t100_live_v40_slot_reservations
                        SET state=$1 WHERE signal_id=$2
                    """, target_slot, stage.signal_id)
                    await db.execute("""
                        UPDATE public.t100_live_order_intents
                        SET state=$1 WHERE signal_id=$2
                    """, target_intent, stage.signal_id)
                witnessed = await reader.inspect([stage])
                self.assertTrue(witnessed.all_claims_exact_and_durable)
                self.assertEqual(
                    witnessed.status, "DISPATCH_CLAIMS_PRESENT_RECONCILE_ONLY"
                )
                self.assertFalse(witnessed.resubmission_allowed)

    async def test_restricted_claim_reader_rejects_privileged_and_mismatched_rows(self):
        from app.t100_live_four_slot_dispatch_claim_reader import (
            PostgresFourSlotDispatchClaimReader,
            DispatchClaimRecoveryHalted,
        )
        from dataclasses import replace

        stage = self.batch.entries[0].stage
        with self.assertRaisesRegex(
            DispatchClaimRecoveryHalted, "RESTRICTED_CONTEXT_REQUIRED",
        ):
            await PostgresFourSlotDispatchClaimReader(self.pool).inspect(
                [stage],
            )
        reader = await self.restricted_dispatch_claim_reader()
        for bad in (
            [replace(stage, signal_id=-1)],
            [stage, stage],
            [replace(stage, state="UNKNOWN")],
        ):
            with self.assertRaisesRegex(
                DispatchClaimRecoveryHalted,
                "STAGED_IDENTITIES_INVALID",
            ):
                await reader.inspect(bad)

        # CI-only seed an inconsistent immutable claim. A row's existence
        # is not enough: exact origin and slot ownership must also match.
        async with self.pool.acquire() as db:
            await db.execute("""
                INSERT INTO public.t100_live_v40_dispatch_claims
                (signal_id,external_oid,holder_id,fencing_token)
                VALUES($1, 'wrong-origin', 'worker-A', 11)
            """, stage.signal_id)
        result = await reader.inspect([stage])
        self.assertEqual(result.status, "DISPATCH_CLAIM_JOURNAL_DRIFT_NO_RETRY")
        self.assertFalse(result.resubmission_allowed)
        self.assertFalse(result.all_claims_exact_and_durable)

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


    async def test_v40_dispatch_readiness_gate_tracks_real_sql_stage_claim_and_ack(self):
        """Read-only gate sees unresolved states and a protected claim outcome."""
        from app.t100_live_v40_state_gate import (
            assess_v40_unresolved_dispatch_state,
        )
        from app.t100_live_four_slot_dispatch_claims import (
            PostgresFourSlotDispatchClaims,
        )
        from app.t100_live_leader_fencing import LeaderLease

        staged = await assess_v40_unresolved_dispatch_state(self.pool)
        self.assertEqual(staged["status"],
                         "V40_DB_DISPATCH_OR_RESERVATION_UNRESOLVED")
        self.assertEqual(staged["unresolved_reservations"], 1)
        self.assertEqual(staged["unresolved_dispatch_claims"], 0)
        self.assertFalse(staged["v40_unresolved_dispatch_state_absent"])

        entry = self.batch.entries[0]
        leader = LeaderLease("t100-live-executor", "worker-A", 11, 45)
        await PostgresFourSlotDispatchClaims(self.pool).claim(
            stage=entry.stage, lease=leader,
        )
        claimed = await assess_v40_unresolved_dispatch_state(self.pool)
        self.assertEqual(claimed["unresolved_reservations"], 1)
        self.assertEqual(claimed["unresolved_dispatch_claims"], 1)
        self.assertFalse(claimed["live_authorization"])

        committed = await self.executor.persist_claimed(
            batch=self.batch, pre_attempt_witness=self.witness,
            lease=leader, observed_at=self.observed_at,
        )
        self.assertTrue(committed.db_atomic_commit_observed)
        cleared = await assess_v40_unresolved_dispatch_state(self.pool)
        self.assertEqual(
            cleared["status"], "V40_DB_STATE_CLEAR_NONAUTHORIZING",
        )
        self.assertTrue(cleared["v40_unresolved_dispatch_state_absent"])
        self.assertEqual(cleared["unresolved_reservations"], 0)
        self.assertEqual(cleared["unresolved_dispatch_claims"], 0)
        self.assertEqual(cleared["unclaimed_protected_reservations"], 0)
        self.assertEqual(cleared["dispatch_claim_provenance_mismatches"], 0)
        self.assertEqual(cleared["protected_reservations"], 1)
        self.assertEqual(cleared["total_dispatch_claims"], 1)
        self.assertFalse(cleared["entry_allowed"])
        self.assertFalse(cleared["live_authorization"])
        self.assertFalse(cleared["mexc_orders_sent"])

    async def test_one_shot_dispatch_claim_is_durable_and_never_reclaimed(self):
        """Claim is NOT order permission; no code in this test sends a POST."""
        from app.t100_live_four_slot_dispatch_claims import (
            PostgresFourSlotDispatchClaims, FourSlotDispatchClaimHalted,
        )
        from app.t100_live_leader_fencing import LeaderLease

        entry = self.batch.entries[0]
        leader = LeaderLease("t100-live-executor", "worker-A", 11, 45)
        store = PostgresFourSlotDispatchClaims(self.pool)
        result = await store.claim(stage=entry.stage, lease=leader)
        self.assertEqual(result.state, "POST_POSSIBLE_UNRESOLVED")
        self.assertTrue(result.reconciliation_required)
        self.assertFalse(result.post_attempted)
        self.assertFalse(result.exchange_post_authorized)
        self.assertFalse(result.live_authorization)
        self.assertFalse(result.mexc_orders_sent)
        self.assertEqual((await self.state())["new_intent"], "SUBMITTING")
        self.assertEqual((await self.state())["slot"], "SUBMITTING")

        async with self.pool.acquire() as db:
            saved = await db.fetchrow("""
                SELECT signal_id, external_oid, holder_id, fencing_token, state
                FROM public.t100_live_v40_dispatch_claims
            """)
            self.assertEqual(saved["signal_id"], entry.intent.signal_id)
            self.assertEqual(saved["external_oid"], entry.intent.external_oid)
            self.assertEqual(saved["holder_id"], "worker-A")
            self.assertEqual(saved["fencing_token"], 11)
            self.assertEqual(saved["state"], "POST_POSSIBLE_UNRESOLVED")

        # Simulate restart: even the SAME leader can never regain permission.
        restarted = PostgresFourSlotDispatchClaims(self.pool)
        with self.assertRaisesRegex(
            FourSlotDispatchClaimHalted, "ALREADY_CLAIMED_RECONCILE_ONLY",
        ):
            await restarted.claim(stage=entry.stage, lease=leader)
        async with self.pool.acquire() as db:
            self.assertEqual(
                await db.fetchval(
                    "SELECT count(*) FROM public.t100_live_v40_dispatch_claims"
                ), 1,
            )

    async def test_one_shot_claim_cannot_be_reset_with_direct_update_or_delete(self):
        """Database-level invariant, even in the privileged disposable CI role."""
        from app.t100_live_four_slot_dispatch_claims import (
            PostgresFourSlotDispatchClaims,
        )
        from app.t100_live_leader_fencing import LeaderLease

        stage = self.batch.entries[0].stage
        store = PostgresFourSlotDispatchClaims(self.pool)
        leader = LeaderLease("t100-live-executor", "worker-A", 11, 45)
        await store.claim(stage=stage, lease=leader)
        async with self.pool.acquire() as db:
            for query in (
                """UPDATE public.t100_live_v40_dispatch_claims
                   SET state='POST_POSSIBLE_UNRESOLVED'
                   WHERE signal_id=$1""",
                """DELETE FROM public.t100_live_v40_dispatch_claims
                   WHERE signal_id=$1""",
            ):
                with self.subTest(query=query.split()[0]):
                    with self.assertRaisesRegex(
                        asyncpg.PostgresError,
                        "T100_DISPATCH_CLAIM_IMMUTABLE",
                    ):
                        await db.execute(query, stage.signal_id)
            saved = await db.fetchrow("""
                SELECT signal_id, external_oid, state, holder_id,
                       fencing_token
                FROM public.t100_live_v40_dispatch_claims
                WHERE signal_id=$1
            """, stage.signal_id)
            self.assertEqual(saved["external_oid"], stage.external_oid)
            self.assertEqual(saved["holder_id"], "worker-A")
            self.assertEqual(saved["fencing_token"], 11)
            self.assertEqual(saved["state"], "POST_POSSIBLE_UNRESOLVED")
            self.assertEqual(await db.fetchval("""
                SELECT count(*) FROM public.t100_live_v40_dispatch_claims
            """), 1)

    async def test_two_competing_dispatch_claims_have_exactly_one_winner(self):
        from app.t100_live_four_slot_dispatch_claims import (
            PostgresFourSlotDispatchClaims, FourSlotDispatchClaimHalted,
        )
        from app.t100_live_leader_fencing import LeaderLease

        stage = self.batch.entries[0].stage
        leader = LeaderLease("t100-live-executor", "worker-A", 11, 45)
        store = PostgresFourSlotDispatchClaims(self.pool)
        answers = await asyncio.gather(
            store.claim(stage=stage, lease=leader),
            store.claim(stage=stage, lease=leader),
            return_exceptions=True,
        )
        self.assertEqual(
            sum(not isinstance(v, Exception) for v in answers), 1
        )
        self.assertEqual(
            sum(isinstance(v, FourSlotDispatchClaimHalted) for v in answers), 1
        )
        async with self.pool.acquire() as db:
            self.assertEqual(await db.fetchval("""
                SELECT count(*) FROM public.t100_live_v40_dispatch_claims
            """), 1)
        self.assertEqual((await self.state())["new_intent"], "SUBMITTING")

    async def test_backend_termination_mid_dispatch_claim_rolls_back_and_never_posts(self):
        """Kill only the disposable DB backend AFTER INSERT, before COMMIT."""
        from app.t100_live_four_slot_dispatch_claims import (
            PostgresFourSlotDispatchClaims, FourSlotDispatchClaimHalted,
        )
        from app.t100_live_leader_fencing import LeaderLease

        require_isolated_ci()
        store = PostgresFourSlotDispatchClaims(self.pool)
        stage = self.batch.entries[0].stage
        leader = LeaderLease("t100-live-executor", "worker-A", 11, 45)
        async with self.pool.acquire() as db:
            await db.execute("""
                CREATE FUNCTION public.t100_ci_kill_dispatch_claim()
                RETURNS trigger LANGUAGE plpgsql AS $claimci$
                BEGIN
                    PERFORM pg_terminate_backend(pg_backend_pid());
                    RETURN NEW;
                END
                $claimci$
            """)
            await db.execute("""
                CREATE TRIGGER t100_ci_kill_claim_after_insert
                AFTER INSERT ON public.t100_live_v40_dispatch_claims
                FOR EACH ROW EXECUTE FUNCTION public.t100_ci_kill_dispatch_claim()
            """)
        try:
            with self.assertRaisesRegex(
                FourSlotDispatchClaimHalted, "DB_FAILURE_RECONCILE_ONLY",
            ):
                await store.claim(stage=stage, lease=leader)
            async with self.pool.acquire() as db:
                self.assertEqual(await db.fetchval("""
                    SELECT count(*) FROM public.t100_live_v40_dispatch_claims
                """), 0)
            self.assertEqual((await self.state())["new_intent"], "SUBMITTING")
        finally:
            async with self.pool.acquire() as db:
                await db.execute("""
                    DROP TRIGGER IF EXISTS t100_ci_kill_claim_after_insert
                    ON public.t100_live_v40_dispatch_claims
                """)
                await db.execute("""
                    DROP FUNCTION IF EXISTS public.t100_ci_kill_dispatch_claim()
                """)

        # The previous failed DB transaction cannot have sent an order:
        # this module is exclusively SQL. A new claim is now permissible
        # only because the old INSERT demonstrably rolled back entirely.
        result = await store.claim(stage=stage, lease=leader)
        self.assertFalse(result.mexc_orders_sent)
        self.assertEqual(result.state, "POST_POSSIBLE_UNRESOLVED")
        async with self.pool.acquire() as db:
            self.assertEqual(await db.fetchval("""
                SELECT count(*) FROM public.t100_live_v40_dispatch_claims
            """), 1)

    async def test_dispatch_claim_rejects_stale_leader_unknown_intent_and_bad_id(self):
        from dataclasses import replace
        from app.t100_live_four_slot_dispatch_claims import (
            PostgresFourSlotDispatchClaims, FourSlotDispatchClaimHalted,
        )
        from app.t100_live_leader_fencing import LeaderLease

        stage = self.batch.entries[0].stage
        leader = LeaderLease("t100-live-executor", "worker-A", 11, 45)
        store = PostgresFourSlotDispatchClaims(self.pool)
        with self.assertRaisesRegex(
            FourSlotDispatchClaimHalted, "STAGE_OR_FENCE_INVALID",
        ):
            await store.claim(stage=replace(stage, state="UNKNOWN"), lease=leader)
        with self.assertRaisesRegex(
            FourSlotDispatchClaimHalted, "DURABLE_STAGE_MISMATCH",
        ):
            await store.claim(
                stage=replace(stage, external_oid="altered-origin"),
                lease=leader,
            )
        async with self.pool.acquire() as db:
            await db.execute("""
                UPDATE public.t100_live_order_intents
                SET state='UNKNOWN'
                WHERE signal_id=$1
            """, stage.signal_id)
        with self.assertRaisesRegex(
            FourSlotDispatchClaimHalted, "DURABLE_STAGE_MISMATCH",
        ):
            await store.claim(stage=stage, lease=leader)
        async with self.pool.acquire() as db:
            await db.execute("""
                UPDATE public.t100_live_order_intents
                SET state='SUBMITTING' WHERE signal_id=$1
            """, stage.signal_id)
            await db.execute("""
                UPDATE public.t100_live_executor_leases
                SET fencing_token=12
                WHERE lease_name='t100-live-executor'
            """)
        with self.assertRaisesRegex(
            FourSlotDispatchClaimHalted, "LEASE_STALE",
        ):
            await store.claim(stage=stage, lease=leader)
        async with self.pool.acquire() as db:
            self.assertEqual(await db.fetchval("""
                SELECT count(*) FROM public.t100_live_v40_dispatch_claims
            """), 0)

    async def test_claim_bound_commit_rejects_missing_or_wrong_dispatch_origin(self):
        """The final SQL writer itself refuses a non-claimed bundle.

        No venue client is involved; a prepared bundle supplied by a caller
        must never override the database's immutable claim ownership.
        """
        from app.t100_live_leader_fencing import LeaderLease

        entry = self.batch.entries[0]
        leader = LeaderLease("t100-live-executor", "worker-A", 11, 45)
        before = await self.state()

        with self.assertRaisesRegex(
            ProtectedCommitHalted, "DISPATCH_CLAIM_UNVERIFIED",
        ):
            await self.executor.persist_claimed(
                batch=self.batch,
                pre_attempt_witness=self.witness, lease=leader,
                observed_at=self.observed_at,
            )
        self.assertEqual(await self.state(), before)

        # Exact signal, wrong origin: even privileged direct CI SQL
        # cannot manufacture a claim matching the observed fill.
        async with self.pool.acquire() as db:
            await db.execute("""
                INSERT INTO public.t100_live_v40_dispatch_claims
                (signal_id, external_oid, holder_id, fencing_token)
                VALUES($1, 'foreign-origin', 'worker-A', 11)
            """, entry.intent.signal_id)
        with self.assertRaisesRegex(
            ProtectedCommitHalted, "DISPATCH_CLAIM_UNVERIFIED",
        ):
            await self.executor.persist_claimed(
                batch=self.batch, pre_attempt_witness=self.witness,
                lease=leader, observed_at=self.observed_at,
            )
        self.assertEqual(await self.state(), before)
        async with self.pool.acquire() as db:
            self.assertEqual(await db.fetchval("""
                SELECT count(*) FROM public.t100_live_order_fills
            """), 0)

    async def test_claim_bound_restart_reconciles_private_get_then_real_postgres_commit(self):
        """True restricted-SQL claim + mock GET + actual DB atomic commit.

        The mock exchange positions/stops/fills are NOT a live venue test.
        No future real transport is connected to this procedure.
        """
        from decimal import Decimal
        from app.t100_live_four_slot_mexc_readonly import (
            inspect_claimed_four_slot_prepared_get_only,
        )
        from app.t100_live_four_slot_dispatch_claims import (
            PostgresFourSlotDispatchClaims,
        )
        from app.t100_live_leader_fencing import LeaderLease
        from tests.test_t100_live_four_slot_mexc_readonly import MockMexcGetOnly
        from tests.test_t100_live_v40_protected_finalization import deal

        entry = self.batch.entries[0]
        positions = [dict(x) for x in self.position_rows]
        stops = [dict(x) for x in self.stop_rows]
        for row in positions:
            row.update({"im": Decimal("10"), "unRealizedPnl": Decimal("0")})
        client = MockMexcGetOnly([self.new_order], positions, stops)
        client.deals[entry.order_id] = [deal()]
        before = await self.state()
        reader = await self.restricted_dispatch_claim_reader()

        # No durable POST-possibility claim = no exchange read, no bundle
        # and no database write, even if the mock venue contains a fill.
        rejected = await inspect_claimed_four_slot_prepared_get_only(
            stages=[entry.stage], intents=[entry.intent],
            pre_attempt_witness=self.witness, claim_reader=reader,
            client=client,
        )
        self.assertEqual(
            rejected.status, "PROTECTED_GET_DISPATCH_CLAIM_NOT_VERIFIED_NO_RETRY",
        )
        self.assertIsNone(rejected.prepared_batch)
        self.assertEqual(client.calls, [])
        self.assertEqual(await self.state(), before)

        leader = LeaderLease("t100-live-executor", "worker-A", 11, 45)
        claimed = await PostgresFourSlotDispatchClaims(self.pool).claim(
            stage=entry.stage, lease=leader,
        )
        self.assertFalse(claimed.exchange_post_authorized)
        self.assertFalse(claimed.mexc_orders_sent)

        prepared = await inspect_claimed_four_slot_prepared_get_only(
            stages=[entry.stage], intents=[entry.intent],
            pre_attempt_witness=self.witness, claim_reader=reader,
            client=client,
        )
        self.assertTrue(prepared.dispatch_claims_verified)
        self.assertEqual(
            prepared.status, "PROTECTED_GET_BATCH_PREPARED_NONAUTHORIZING",
        )
        self.assertEqual(prepared.deal_histories_observed, 1)
        self.assertIsNotNone(prepared.prepared_batch)
        self.assertEqual(await self.state(), before)
        self.assertTrue(all("/private/" in path for path, _ in client.calls))
        self.assertFalse(prepared.entry_allowed)
        self.assertFalse(prepared.live_authorization)

        saved = await self.executor.persist_claimed(
            batch=prepared.prepared_batch,
            pre_attempt_witness=self.witness,
            lease=leader,
            observed_at=self.observed_at,
        )
        self.assertTrue(saved.db_atomic_commit_observed)
        self.assertFalse(saved.mexc_orders_sent)
        self.assertEqual(await self.state(), {
            "new_intent": "ACK", "slot": "PROTECTED",
            "fills": 1, "fill_sets": 1, "checkpoints": 4,
            "revisions": 4,
        })
        async with self.pool.acquire() as db:
            self.assertEqual(await db.fetchval("""
                SELECT count(*) FROM public.t100_live_v40_dispatch_claims
                WHERE signal_id=$1 AND external_oid=$2
            """, entry.intent.signal_id, entry.intent.external_oid), 1)

    async def test_real_postgres_commit_requires_fresh_two_bookend_private_get_fill_evidence(self):
        """One test crosses mocked PRIVATE GETs into REAL disposable PostgreSQL.

        The exchange is a fixture: never MEXC POST or real native stops.
        Restart from actual persisted rows is exercised in the companion
        test_real_postgres_atomic_commit_and_restart_recovery.
        """
        from decimal import Decimal
        from app.t100_live_four_slot_mexc_readonly import (
            inspect_staged_four_slot_prepared_get_only,
        )
        from tests.test_t100_live_four_slot_mexc_readonly import MockMexcGetOnly
        from tests.test_t100_live_v40_protected_finalization import deal
        from app.t100_live_leader_fencing import LeaderLease

        entry = self.batch.entries[0]
        positions = [dict(x) for x in self.position_rows]
        stops = [dict(x) for x in self.stop_rows]
        for row in positions:
            row.update({"im": Decimal("10"), "unRealizedPnl": Decimal("0")})
        client = MockMexcGetOnly([self.new_order], positions, stops)
        client.deals[entry.order_id] = [deal()]
        before = await self.state()
        self.assertEqual(before["new_intent"], "SUBMITTING")
        self.assertEqual(before["slot"], "SUBMITTING")

        # Exchange observation cannot write ACK or a new checkpoint.
        observed = await inspect_staged_four_slot_prepared_get_only(
            stages=[entry.stage], intents=[entry.intent],
            pre_attempt_witness=self.witness, client=client,
        )
        self.assertEqual(
            observed.status, "PROTECTED_GET_BATCH_PREPARED_NONAUTHORIZING",
        )
        self.assertEqual(observed.orders_observed, 1)
        self.assertEqual(observed.deal_histories_observed, 1)
        self.assertEqual(
            sum("/private/order/deal_details/" in path
                for path, _ in client.calls), 2,
        )
        self.assertTrue(all("/private/" in path for path, _ in client.calls))
        self.assertFalse(observed.live_authorization)
        self.assertFalse(observed.mexc_orders_sent)
        self.assertEqual(await self.state(), before)

        # The fresh GET-backed bundle, not self.batch from an offline helper,
        # is the ONLY input to actual PostgreSQL ACK+fill/checkpoint writes.
        batch = observed.prepared_batch
        self.assertIsNotNone(batch)
        saved = await self.executor.persist(
            batch=batch, pre_attempt_witness=self.witness,
            lease=LeaderLease("t100-live-executor", "worker-A", 11, 45),
            observed_at=self.observed_at,
        )
        self.assertEqual(
            saved.status,
            "V40_PROTECTED_RECORDS_ATOMICALLY_PERSISTED_NONAUTHORIZING",
        )
        self.assertFalse(saved.live_authorization)
        self.assertEqual(await self.state(), {
            "new_intent": "ACK", "slot": "PROTECTED",
            "fills": 1, "fill_sets": 1, "checkpoints": 4, "revisions": 4,
        })
        async with self.pool.acquire() as db:
            persisted = await db.fetchrow("""
                SELECT order_id, fill_digest_sha256, fill_count
                FROM public.t100_live_order_fill_sets WHERE order_id=$1
            """, entry.order_id)
            self.assertEqual(persisted["order_id"], entry.order_id)
            self.assertEqual(
                persisted["fill_digest_sha256"],
                batch.entries[0].fill_set.fill_digest_sha256,
            )
            self.assertEqual(persisted["fill_count"], 1)
        with self.assertRaises(ProtectedCommitHalted):
            await self.executor.persist(
                batch=batch, pre_attempt_witness=self.witness,
                lease=LeaderLease("t100-live-executor", "worker-A", 11, 45),
                observed_at=self.observed_at,
            )
        self.assertEqual((await self.state())["fills"], 1)

    async def test_real_postgres_stays_unmodified_after_invalid_private_get_outcomes(self):
        """Every GET failure blocks before any real database ACK/INSERT."""
        from decimal import Decimal
        from app.t100_live_four_slot_mexc_readonly import (
            inspect_staged_four_slot_prepared_get_only,
        )
        from tests.test_t100_live_four_slot_mexc_readonly import MockMexcGetOnly
        from tests.test_t100_live_v40_protected_finalization import deal

        entry = self.batch.entries[0]
        positions = [dict(x) for x in self.position_rows]
        stops = [dict(x) for x in self.stop_rows]
        for row in positions:
            row.update({"im": Decimal("10"), "unRealizedPnl": Decimal("0")})
        baseline = await self.state()

        for mode in ("missing_order", "no_deals", "partial_deal",
                     "missing_stop", "timeout"):
            with self.subTest(mode=mode):
                client = MockMexcGetOnly([self.new_order], positions, stops)
                client.deals[entry.order_id] = [deal()]
                if mode == "missing_order":
                    client.orders.clear()
                elif mode == "no_deals":
                    client.deals.clear()
                elif mode == "partial_deal":
                    client.deals[entry.order_id] = [
                        deal(vol=Decimal("249")),
                    ]
                elif mode == "missing_stop":
                    client.stops.pop()
                else:
                    client.fail_path = (
                        "/api/v1/private/order/deal_details/" + entry.order_id
                    )
                observed = await inspect_staged_four_slot_prepared_get_only(
                    stages=[entry.stage], intents=[entry.intent],
                    pre_attempt_witness=self.witness, client=client,
                )
                self.assertIsNone(observed.prepared_batch)
                self.assertNotEqual(
                    observed.status,
                    "PROTECTED_GET_BATCH_PREPARED_NONAUTHORIZING",
                )
                self.assertFalse(observed.entry_allowed)
                self.assertFalse(observed.mexc_orders_sent)
                self.assertEqual(await self.state(), baseline)
                self.assertTrue(
                    all("/private/" in path for path, _ in client.calls),
                )

    async def test_restricted_postgres_plus_get_only_four_slot_recovery_and_missing_stop(self):
        """Exercise actual read-only DB SELECTs and MEXC GET contracts TOGETHER.

        Creates an explicitly SELECT-only role in the disposable CI
        PostgreSQL service. There is no exchange client, API credential,
        live executor, Render deployment, order POST or production grant.
        """
        from decimal import Decimal
        from app.t100_live_role_preflight import TARGET_ROLE
        from app.t100_live_four_slot_ledger_reader import (
            PostgresFourSlotLedgerReader,
        )
        from app.t100_live_four_slot_readonly_cli import (
            run_readonly_four_slot_audit,
        )
        from tests.test_t100_live_four_slot_mexc_readonly import (
            MockMexcGetOnly,
        )

        await self.finalize()
        async with self.pool.acquire() as db:
            await db.execute("""
                DO $ci$
                BEGIN
                  IF NOT EXISTS (
                    SELECT 1 FROM pg_roles
                    WHERE rolname='t100_live_executor_runtime'
                  ) THEN
                    CREATE ROLE t100_live_executor_runtime
                    LOGIN PASSWORD 't100-ci-only-local-postgres'
                    NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION;
                  END IF;
                END $ci$;
            """)
            await db.execute(
                "GRANT CONNECT ON DATABASE mexc_exhaustion TO t100_live_executor_runtime"
            )
            await db.execute(
                "GRANT USAGE ON SCHEMA public TO t100_live_executor_runtime"
            )
            await db.execute("""
                GRANT SELECT ON
                    public.t100_live_position_checkpoints,
                    public.t100_live_position_checkpoint_revisions,
                    public.t100_live_order_intents,
                    public.t100_live_stop_amendments
                TO t100_live_executor_runtime
            """)
        reader_pool = await asyncpg.create_pool(
            host="127.0.0.1", port=5432,
            user=TARGET_ROLE, password=TEST_PASSWORD,
            database=EXPECTED_DATABASE, min_size=1, max_size=1,
            timeout=5,
            server_settings={"default_transaction_read_only": "on"},
        )
        try:
            async with reader_pool.acquire() as db:
                self.assertEqual(
                    await db.fetchval("SELECT current_user"), TARGET_ROLE
                )
                self.assertEqual(
                    await db.fetchval(
                        "SELECT has_table_privilege(current_user,"
                        "'public.t100_live_position_checkpoints','INSERT')"
                    ), False
                )
                self.assertEqual(
                    await db.fetchval(
                        "SELECT current_setting('default_transaction_read_only')"
                    ), "on"
                )
            _, old_orders, _, _ = fixture(3)
            positions = [dict(x) for x in self.position_rows]
            stops = [dict(x) for x in self.stop_rows]
            for row in positions:
                row.update({
                    "im": Decimal("10"), "unRealizedPnl": Decimal("0"),
                })
            orders = [*old_orders, self.new_order]
            env = {
                "T100_EXECUTION_MODE": "paper",
                "T100_LIVE_ORDER_API_ENABLED": "false",
                "T100_LIVE_KILL_SWITCH": "true",
            }
            ledger = PostgresFourSlotLedgerReader(reader_pool)
            client = MockMexcGetOnly(orders, positions, stops)
            recovered = await run_readonly_four_slot_audit(
                ledger=ledger, client=client, env=env,
            )
            self.assertEqual(
                recovered["status"],
                "NONFLAT_PROVENANCE_MATCHED_NONAUTHORIZING",
            )
            self.assertEqual(recovered["owned_four_slot_positions_observed"], 4)
            self.assertFalse(recovered["live_authorization"])
            self.assertFalse(recovered["entry_allowed"])
            self.assertFalse(recovered["database_writes_sent"])
            self.assertFalse(recovered["mexc_orders_sent"])
            self.assertGreaterEqual(
                len([x for x in client.calls
                     if x[0].startswith("/api/v1/private/order/external/")]), 4,
            )
            # A stop removed from the GET-only fixture must never be
            # interpreted as a safe or partly owned account.
            no_stop_client = MockMexcGetOnly(orders, positions, stops[:-1])
            failed = await run_readonly_four_slot_audit(
                ledger=ledger, client=no_stop_client, env=env,
            )
            self.assertEqual(failed["status"], "POSITION_STOP_COUNT_MISMATCH")
            self.assertIsNone(failed["owned_four_slot_positions_observed"])
            self.assertEqual(
                failed["stop_count_diagnostic"]
                ["positions_without_positive_stop_loss_row"], 1,
            )
            self.assertFalse(failed["entry_allowed"])
            # A fifth, independently held position must never be adopted.
            manual = dict(positions[-1])
            manual.update({
                "positionId": "manual-position-5000",
                "symbol": "UNOWNED_USDT",
            })
            extra_client = MockMexcGetOnly(orders, [*positions, manual], stops)
            foreign = await run_readonly_four_slot_audit(
                ledger=ledger, client=extra_client, env=env,
            )
            self.assertEqual(foreign["status"], "ACCOUNT_EXCEEDS_FOUR_SLOTS")
            self.assertIsNone(foreign["owned_four_slot_positions_observed"])
            self.assertFalse(foreign["live_authorization"])
        finally:
            await reader_pool.close()

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



    async def test_real_postgres_reserve_stage_get_commit_and_restart_fourth_slot(self):
        """One entire real-SQL 4th-slot lifecycle; exchange remains a GET mock.

        Unlike the setup's pre-seeded SUBMITTING row, this test creates the
        reservation + durable intent with the actual transaction writers.
        It then proves a GET-only full fill/stop witness before real SQL
        ACK/fill/checkpoint writes and read-only nonflat restart recovery.
        """
        from decimal import Decimal as D
        from app.t100_live_execution import EntryIntent
        from app.t100_live_four_slot_reservations import PostgresFourSlotReservations
        from app.t100_live_four_slot_attempt_journal import PostgresFourSlotAttemptJournal
        from app.t100_live_four_slot_ledger_reader import PostgresFourSlotLedgerReader
        from app.t100_live_four_slot_mexc_readonly import (
            inspect_existing_four_slots_get_only,
            inspect_staged_four_slot_prepared_get_only,
        )
        from app.t100_live_four_slot_planner import plan_four_slot_v40
        from app.t100_live_leader_fencing import LeaderLease
        from tests.test_t100_live_four_slot_planner import prior, OPEN_TS
        from tests.test_t100_live_four_slot_mexc_readonly import (
            MockMexcGetOnly, prepare_rows,
        )
        from tests.test_t100_live_v40_protected_finalization import deal

        setup_entry = self.batch.entries[0]
        async with self.pool.acquire() as db:
            await db.execute("""
                DELETE FROM public.t100_live_v40_slot_reservations
                WHERE signal_id=$1
            """, setup_entry.intent.signal_id)
            await db.execute("""
                DELETE FROM public.t100_live_order_intents
                WHERE signal_id=$1
            """, setup_entry.intent.signal_id)
        start = await self.state()
        self.assertIsNone(start["slot"])
        self.assertIsNone(start["new_intent"])
        self.assertEqual(start["checkpoints"], 3)

        # A real ledger witness MUST use the restricted, read-only DB role.
        # Connecting with the administrative/migration role correctly fails.
        from app.t100_live_role_preflight import TARGET_ROLE
        async with self.pool.acquire() as db:
            await db.execute("""
                DO $ci$
                BEGIN
                  IF NOT EXISTS (
                    SELECT 1 FROM pg_roles
                    WHERE rolname='t100_live_executor_runtime'
                  ) THEN
                    CREATE ROLE t100_live_executor_runtime
                    LOGIN PASSWORD 't100-ci-only-local-postgres'
                    NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION;
                  END IF;
                END $ci$;
            """)
            await db.execute(
                "GRANT CONNECT ON DATABASE mexc_exhaustion TO t100_live_executor_runtime"
            )
            await db.execute(
                "GRANT USAGE ON SCHEMA public TO t100_live_executor_runtime"
            )
            await db.execute("""
                GRANT SELECT ON
                    public.t100_live_position_checkpoints,
                    public.t100_live_position_checkpoint_revisions,
                    public.t100_live_order_intents,
                    public.t100_live_stop_amendments
                TO t100_live_executor_runtime
            """)
        self.read_pool = await asyncpg.create_pool(
            host="127.0.0.1", port=5432,
            user=TARGET_ROLE, password=TEST_PASSWORD,
            database=EXPECTED_DATABASE, min_size=1, max_size=1,
            timeout=5,
            server_settings={"default_transaction_read_only": "on"},
        )
        _, old_orders, old_positions, old_stops = prepare_rows(3)
        ledger = PostgresFourSlotLedgerReader(self.read_pool)
        client_before = MockMexcGetOnly(old_orders, old_positions, old_stops)
        witness = await inspect_existing_four_slots_get_only(
            ledger=ledger, client=client_before,
        )
        self.assertEqual(witness.status,
                         "NONFLAT_PROVENANCE_MATCHED_NONAUTHORIZING")
        self.assertEqual(witness.occupied_slots, 3)
        self.assertFalse(witness.live_authorization)

        plan = plan_four_slot_v40(
            inventory=witness.inventory,
            durable_recovery=witness.recovery,
            symbol="FOURTH_USDT", prior_bars=prior(),
            admission_open_ts=OPEN_TS,
            stage1_atr7_pct=6., equity_usdt=10000.,
            marked_existing_usdt=0.,
        )
        self.assertEqual(plan.action, "ADMIT")
        leader = LeaderLease("t100-live-executor", "worker-A", 11, 45)
        moment = datetime.now(timezone.utc)
        reservation = await PostgresFourSlotReservations(self.pool).reserve(
            plan=plan, witness=witness, observed_at=moment,
            lease=leader, signal_id=setup_entry.intent.signal_id,
            symbol="FOURTH_USDT",
        )
        self.assertEqual((await self.state())["slot"], "RESERVED")
        entry = EntryIntent(
            signal_id=reservation.signal_id, symbol=reservation.symbol,
            reference_price=D("2"), notional_usdt=D("500"),
            volume_contracts=D("250"), stop_price=D("3.5"),
            external_oid=reservation.external_oid,
        )
        stage = await PostgresFourSlotAttemptJournal(self.pool).stage(
            intent=entry, witness=witness, lease=leader,
            observed_at=moment,
            contract_size=D("1"), volume_step=D("1"),
            price_tick=D("0.01"),
        )
        self.assertEqual(stage.state, "SUBMITTING")
        staged_state = await self.state()
        self.assertEqual(staged_state["slot"], "SUBMITTING")
        self.assertEqual(staged_state["new_intent"], "SUBMITTING")
        self.assertEqual(staged_state["fills"], 0)

        after_positions = [dict(x) for x in self.position_rows]
        after_stops = [dict(x) for x in self.stop_rows]
        for row in after_positions:
            row.update({"im": D("10"), "unRealizedPnl": D("0")})
        simulated_order = dict(self.new_order)
        simulated_order["externalOid"] = entry.external_oid
        simulated_order["feeCurrency"] = "USDT"
        client_after = MockMexcGetOnly(
            [simulated_order], after_positions, after_stops,
        )
        client_after.deals[str(simulated_order["orderId"])] = [deal()]
        observed = await inspect_staged_four_slot_prepared_get_only(
            stages=[stage], intents=[entry],
            pre_attempt_witness=witness, client=client_after,
        )
        self.assertEqual(
            observed.status, "PROTECTED_GET_BATCH_PREPARED_NONAUTHORIZING",
        )
        self.assertEqual(await self.state(), staged_state)
        self.assertFalse(observed.mexc_orders_sent)

        saved = await self.executor.persist(
            batch=observed.prepared_batch,
            pre_attempt_witness=witness, lease=leader, observed_at=moment,
        )
        self.assertTrue(saved.db_atomic_commit_observed)
        self.assertFalse(saved.live_authorization)
        self.assertEqual(await self.state(), {
            "new_intent": "ACK", "slot": "PROTECTED",
            "fills": 1, "fill_sets": 1, "checkpoints": 4, "revisions": 4,
        })
        # Read all durable rows from PostgreSQL, independently re-GET
        # origins/positions/stops and reconstruct four owned protected slots.
        old_orders.append(simulated_order)
        client_restart = MockMexcGetOnly(
            old_orders, after_positions, after_stops,
        )
        restored = await inspect_existing_four_slots_get_only(
            ledger=ledger, client=client_restart,
        )
        self.assertEqual(
            restored.status, "NONFLAT_PROVENANCE_MATCHED_NONAUTHORIZING",
        )
        self.assertEqual(restored.occupied_slots, 4)
        self.assertFalse(restored.live_authorization)
        self.assertTrue(
            all("/private/" in path for path, _ in
                [*client_before.calls, *client_after.calls,
                 *client_restart.calls])
        )

    async def test_terminated_postgres_backend_rolls_back_partial_protected_commit(self):
        """Kill only the CI backend mid-commit; another connection sees no ACK.

        A database exception is not the same as losing its connection after
        some writes. The single transaction must roll back the ACK/fills
        even when a backend is terminated during checkpoint insertion.
        This trigger is installed only in the guarded disposable CI DB.
        """
        require_isolated_ci()
        async with self.pool.acquire() as db:
            await db.execute("""
                CREATE FUNCTION public.t100_ci_terminate_during_checkpoint()
                RETURNS trigger LANGUAGE plpgsql AS $t100ci$
                BEGIN
                    PERFORM pg_terminate_backend(pg_backend_pid());
                    RETURN NEW;
                END
                $t100ci$
            """)
            await db.execute("""
                CREATE TRIGGER t100_ci_terminate_checkpoint
                BEFORE INSERT ON public.t100_live_position_checkpoints
                FOR EACH ROW
                EXECUTE FUNCTION public.t100_ci_terminate_during_checkpoint()
            """)
        before = await self.state()
        self.assertEqual(before["new_intent"], "SUBMITTING")
        try:
            with self.assertRaises(ProtectedCommitHalted):
                await self.finalize()
            # A fresh pool checkout must observe a fully rolled-back TX,
            # not an ACK, fill or partially persisted stop/checkpoint.
            self.assertEqual(await self.state(), before)
        finally:
            async with self.pool.acquire() as db:
                await db.execute("""
                    DROP TRIGGER IF EXISTS t100_ci_terminate_checkpoint
                    ON public.t100_live_position_checkpoints
                """)
                await db.execute("""
                    DROP FUNCTION IF EXISTS public.t100_ci_terminate_during_checkpoint()
                """)
        self.assertTrue((await self.finalize()).db_atomic_commit_observed)
        self.assertEqual((await self.state())["new_intent"], "ACK")

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
