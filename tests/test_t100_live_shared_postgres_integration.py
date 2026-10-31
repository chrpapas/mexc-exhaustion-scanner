"""REAL disposable PostgreSQL 16 shared manual/T100 V40 transaction chain.

Executes only under the existing ephemeral CI localhost PostgreSQL guard.
Uses MOCKED exchange GET evidence, zero MEXC credentials, NO order POST and
no connection to Render or production databases.

Exercises the actual PostgreSQL transaction, advisory lock, durable intent
and fill/checkpoint INSERTs, rollback and read-only restart SELECTs when
there are two operator positions (manual CROSS short + ISOLATED long)
without operator stops. Operator positions have no T100 DB checkpoints.
Success proves database mechanics in disposable CI, NOT venue atomicity,
wallet collateral, safe margin, stop execution or production readiness.
"""
from __future__ import annotations

import asyncio
import copy
from datetime import datetime, timezone
from decimal import Decimal as D
import unittest

import asyncpg

from app.t100_live_four_slot_planner import plan_shared_four_slot_v40
from app.t100_live_four_slot_reservations import (
    FourSlotReservationHalted, PostgresFourSlotReservations,
)
from app.t100_live_four_slot_attempt_journal import (
    FourSlotAttemptHalted, PostgresFourSlotAttemptJournal,
)
from app.t100_live_four_slot_ledger_reader import PostgresFourSlotLedgerReader
from app.t100_live_four_slot_snapshot_coordinator import FourSlotSnapshotCoordinator
from app.t100_live_leader_fencing import LeaderLease
from app.t100_live_execution import EntryIntent
from app.t100_live_shared_protected_finalization import (
    prepare_shared_protected_entry_offline,
)
from app.t100_live_shared_restart_recovery import (
    verify_shared_restart_protected_entry,
)
from app.t100_live_v40_protected_commit import (
    PostgresProtectedV40Commit, ProtectedCommitHalted,
)
from app.t100_live_role_preflight import TARGET_ROLE

from tests.test_t100_live_v40_postgres_integration import (
    EXPECTED_DATABASE, MIGRATIONS, ROOT, TEST_PASSWORD, require_isolated_ci,
)
from tests.test_t100_live_four_slot_reservations import lease
from tests.test_t100_live_four_slot_planner import OPEN_TS, prior
from tests.test_t100_live_four_slot_snapshot_coordinator import (
    Completeness, Venue,
)
from tests.test_t100_live_shared_protected_entry_observer import (
    SharedProtectedEntryTests, counts,
)
from tests.test_t100_live_shared_protected_finalization import deal


class RealPostgresSharedManualV40Tests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        require_isolated_ci()
        self.pool = await asyncpg.create_pool(
            host="127.0.0.1", port=5432, user="postgres",
            password=TEST_PASSWORD, database=EXPECTED_DATABASE,
            min_size=1, max_size=5, timeout=5,
        )
        self.read_pool = None
        async with self.pool.acquire() as db:
            if await db.fetchval("SELECT current_database()") != EXPECTED_DATABASE:
                raise RuntimeError("SHARED_CI_POSTGRES_DATABASE_MISMATCH")
            if await db.fetchval(
                "SELECT to_regclass('public.t100_live_v40_slot_reservations')"
            ) is None:
                for migration in MIGRATIONS:
                    await db.execute(
                        (ROOT / "migrations" / migration).read_text()
                    )
            await db.execute("""
                TRUNCATE TABLE
                  public.t100_live_v40_slot_reservations,
                  public.t100_live_position_checkpoints,
                  public.t100_live_position_checkpoint_revisions,
                  public.t100_live_order_intents,
                  public.t100_live_order_fill_sets,
                  public.t100_live_order_fills,
                  public.t100_live_stop_amendments,
                  public.t100_live_executor_leases
                RESTART IDENTITY CASCADE
            """)
            await db.execute("""
                INSERT INTO public.t100_live_executor_leases
                (lease_name,holder_id,fencing_token,lease_expires_at)
                VALUES ('t100-live-executor','worker-A',11,
                        clock_timestamp()+interval '5 minutes')
            """)
        source = SharedProtectedEntryTests()
        await source.asyncSetUp()
        self.source = source
        self.pre = source.pre_witness
        self.manual_positions = copy.deepcopy(source.pre_positions)
        self.manual_stops = copy.deepcopy(source.pre_stops)
        self.original_manual = copy.deepcopy(
            (self.manual_positions, self.manual_stops)
        )
        self.reservation_store = PostgresFourSlotReservations(self.pool)
        self.journal = PostgresFourSlotAttemptJournal(self.pool)
        self.commit_store = PostgresProtectedV40Commit(self.pool)
        self.observed_at = datetime.now(timezone.utc)

    async def asyncTearDown(self):
        if self.read_pool is not None:
            await self.read_pool.close()
        await self.pool.close()

    def plan(self, symbol="NEW_USDT"):
        return plan_shared_four_slot_v40(
            snapshot=self.pre,
            account_positions=self.manual_positions,
            account_stops=self.manual_stops,
            symbol=symbol,
            prior_bars=prior(),
            admission_open_ts=OPEN_TS,
            stage1_atr7_pct=6.0, equity_usdt=10000.0,
            marked_existing_usdt=1000.0,
        )

    async def reserve(self, *, signal_id=401, symbol="NEW_USDT"):
        return await self.reservation_store.reserve(
            plan=self.plan(symbol), witness=self.pre,
            shared_pre_positions=self.manual_positions,
            shared_pre_stops=self.manual_stops,
            observed_at=self.observed_at,
            lease=lease(), signal_id=signal_id, symbol=symbol,
        )

    def intent(self, reservation):
        return EntryIntent(
            signal_id=reservation.signal_id, symbol=reservation.symbol,
            reference_price=D("2"), notional_usdt=D("500"),
            volume_contracts=D("250"), stop_price=D("3.5"),
            external_oid=reservation.external_oid,
        )

    async def stage(self, reservation):
        intent = self.intent(reservation)
        stage = await self.journal.stage(
            intent=intent, witness=self.pre, lease=lease(),
            observed_at=self.observed_at,
            shared_pre_positions=self.manual_positions,
            shared_pre_stops=self.manual_stops,
            contract_size=D("1"), volume_step=D("1"),
            price_tick=D("0.01"),
        )
        return intent, stage

    def prepared(self, intent, stage):
        order = copy.deepcopy(self.source.order)
        order["externalOid"] = intent.external_oid
        order["feeCurrency"] = "USDT"
        return prepare_shared_protected_entry_offline(
            pre_witness=self.pre,
            pre_positions=self.manual_positions,
            pre_stops=self.manual_stops,
            stage=stage, intent=intent,
            post_positions=copy.deepcopy(self.source.post_positions),
            post_stops=copy.deepcopy(self.source.post_stops),
            post_order=order,
            post_inventory=copy.deepcopy(self.source.post_report),
            order_deals=[deal()],
        ), order

    async def snapshot_state(self, oid):
        async with self.pool.acquire() as db:
            return dict(
                state=await db.fetchval("""
                    SELECT state FROM public.t100_live_order_intents
                    WHERE external_oid=$1
                """, oid),
                slot=await db.fetchval("""
                    SELECT state FROM public.t100_live_v40_slot_reservations
                    WHERE external_oid=$1
                """, oid),
                fill_sets=await db.fetchval(
                    "SELECT count(*) FROM public.t100_live_order_fill_sets"
                ),
                fills=await db.fetchval(
                    "SELECT count(*) FROM public.t100_live_order_fills"
                ),
                checkpoints=await db.fetchval(
                    "SELECT count(*) FROM public.t100_live_position_checkpoints"
                ),
                revisions=await db.fetchval("""
                    SELECT count(*) FROM
                    public.t100_live_position_checkpoint_revisions
                """),
            )

    async def allow_readonly_role(self):
        async with self.pool.acquire() as db:
            await db.execute("""
                DO $ci$ BEGIN
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
        async with self.read_pool.acquire() as db:
            self.assertEqual(
                await db.fetchval("SELECT current_user"), TARGET_ROLE,
            )
            self.assertEqual(
                await db.fetchval("""
                    SELECT has_table_privilege(
                      current_user,
                      'public.t100_live_position_checkpoints', 'INSERT'
                    )
                """),
                False,
            )

    async def test_real_postgres_shared_reserve_stage_commit_and_restart(self):
        reserve = await self.reserve()
        self.assertEqual(reserve.occupied_after_reservation, 3)
        self.assertFalse(reserve.entry_allowed)
        intent, stage = await self.stage(reserve)
        self.assertTrue(stage.reconciliation_required)
        self.assertFalse(stage.post_attempted)
        self.assertEqual(
            await self.snapshot_state(reserve.external_oid),
            dict(state="SUBMITTING", slot="SUBMITTING",
                 fill_sets=0, fills=0, checkpoints=0, revisions=0),
        )
        batch, order = self.prepared(intent, stage)
        committed = await self.commit_store.persist(
            batch=batch, pre_attempt_witness=self.pre,
            lease=lease(), observed_at=self.observed_at,
            shared_pre_positions=self.manual_positions,
            shared_pre_stops=self.manual_stops,
        )
        self.assertEqual(committed.entries_committed, 1)
        self.assertTrue(committed.db_atomic_commit_observed)
        self.assertFalse(committed.venue_state_currently_verified)
        self.assertFalse(committed.live_authorization)
        self.assertFalse(committed.mexc_orders_sent)
        self.assertEqual(
            await self.snapshot_state(reserve.external_oid),
            dict(state="ACK", slot="PROTECTED",
                 fill_sets=1, fills=1, checkpoints=1, revisions=1),
        )
        self.assertEqual(
            (self.manual_positions, self.manual_stops), self.original_manual,
        )
        await self.allow_readonly_role()
        fresh = await FourSlotSnapshotCoordinator(
            ledger=PostgresFourSlotLedgerReader(self.read_pool),
            venue=Venue(
                [order],
                copy.deepcopy(self.source.post_positions),
                copy.deepcopy(self.source.post_stops),
            ),
            completeness=Completeness(counts(3, 1)),
        ).inspect(shared_manual_observation=True)
        self.assertEqual(
            fresh.status,
            "SHARED_ACCOUNT_MANAGED_PROVENANCE_MATCHED_NONAUTHORIZING",
        )
        self.assertEqual(fresh.occupied_slots, 1)
        self.assertEqual(fresh.manual_positions_observed, 2)
        self.assertEqual(fresh.account_positions_observed, 3)
        proof = verify_shared_restart_protected_entry(
            pre_witness=self.pre,
            restart_witness=fresh,
            prepared=batch,
            pre_positions=self.manual_positions,
            pre_stops=self.manual_stops,
            restart_positions=self.source.post_positions,
            restart_stops=self.source.post_stops,
            origin_order=order,
        )
        self.assertEqual(
            proof.status,
            "SHARED_RESTART_EXACT_PROTECTED_PROVENANCE_OBSERVED_NONAUTHORIZING",
        )
        self.assertTrue(proof.owned_native_stops_recovered)
        self.assertEqual(proof.unchanged_manual_positions, 2)
        self.assertFalse(proof.real_db_commit_independently_verified)
        self.assertFalse(proof.production_restart_certified)
        self.assertFalse(proof.live_authorization)

    async def test_actual_postgres_trigger_rollback_leaves_submitting(self):
        reserve = await self.reserve()
        intent, stage = await self.stage(reserve)
        batch, _ = self.prepared(intent, stage)
        async with self.pool.acquire() as db:
            await db.execute("""
                CREATE FUNCTION public.t100_ci_shared_abort()
                RETURNS trigger LANGUAGE plpgsql AS $
                BEGIN
                  IF NEW.state='PROTECTED' THEN
                    RAISE EXCEPTION 'CI-only forced protected rollback';
                  END IF;
                  RETURN NEW;
                END $$
            """)
            await db.execute("""
                CREATE TRIGGER t100_ci_shared_abort
                BEFORE UPDATE ON public.t100_live_v40_slot_reservations
                FOR EACH ROW EXECUTE FUNCTION public.t100_ci_shared_abort()
            """)
        before = await self.snapshot_state(reserve.external_oid)
        try:
            with self.assertRaises(ProtectedCommitHalted):
                await self.commit_store.persist(
                    batch=batch, pre_attempt_witness=self.pre,
                    lease=lease(), observed_at=self.observed_at,
                    shared_pre_positions=self.manual_positions,
                    shared_pre_stops=self.manual_stops,
                )
            self.assertEqual(await self.snapshot_state(reserve.external_oid), before)
        finally:
            async with self.pool.acquire() as db:
                await db.execute("""
                    DROP TRIGGER t100_ci_shared_abort
                    ON public.t100_live_v40_slot_reservations
                """)
                await db.execute("DROP FUNCTION public.t100_ci_shared_abort()")
        self.assertEqual(before["state"], "SUBMITTING")
        self.assertEqual(before["slot"], "SUBMITTING")
        self.assertEqual(before["fills"], 0)
        self.assertEqual(before["checkpoints"], 0)

    async def _initialized_shared_stop_test(self):
        from app.t100_live_checkpoint_journal import PostgresCheckpointJournal
        from app.t100_live_checkpoint_coordinator import (
            CheckpointPersistenceCoordinator,
        )
        from app.t100_live_durable_recovery import checkpoint_from_row
        from app.t100_live_stop_amendment_coordinator import (
            StopAmendmentLifecycleCoordinator,
        )
        from app.t100_live_stop_journal import PostgresStopJournal
        from app.t100_live_stop_update_pipeline import (
            StopUpdatePersistencePipeline,
        )
        from app.t100_live_shared_trailing_persistence import (
            SharedTrailingPersistencePipeline,
        )
        from app.t100_live_supervisor import StopProposal
        from app.t100_live_trailing import next_short_stop

        reservation = await self.reserve()
        intent, stage = await self.stage(reservation)
        batch, order = self.prepared(intent, stage)
        await self.commit_store.persist(
            batch=batch, pre_attempt_witness=self.pre,
            lease=lease(), observed_at=self.observed_at,
            shared_pre_positions=self.manual_positions,
            shared_pre_stops=self.manual_stops,
        )
        await self.allow_readonly_role()
        positions = copy.deepcopy(self.source.post_positions)
        old_stops = copy.deepcopy(self.source.post_stops)
        before = await FourSlotSnapshotCoordinator(
            ledger=PostgresFourSlotLedgerReader(self.read_pool),
            venue=Venue([order], positions, old_stops),
            completeness=Completeness(counts(3, 1)),
        ).inspect(shared_manual_observation=True)
        self.assertEqual(
            before.status,
            "SHARED_ACCOUNT_MANAGED_PROVENANCE_MATCHED_NONAUTHORIZING",
        )
        async with self.read_pool.acquire() as db:
            cp_row = await db.fetchrow("""
                SELECT * FROM public.t100_live_position_checkpoints
                WHERE position_id=$1
            """, "390")
        checkpoint = checkpoint_from_row(dict(cp_row))
        self.assertEqual(checkpoint.checkpoint_version, 1)
        proposed = next_short_stop(
            entry=checkpoint.entry_price, low=D("1.72"),
            current_stop=checkpoint.stop_price,
            best_profit_pct=checkpoint.best_profit_pct,
            price_tick=D("0.01"), armed=checkpoint.armed,
        )
        self.assertTrue(proposed.changed)
        self.assertEqual(proposed.stop_price, D("1.73"))
        journal = PostgresStopJournal(self.pool)
        amendments = StopAmendmentLifecycleCoordinator(journal)
        amendment, state = await amendments.prepare(StopProposal(
            symbol=checkpoint.symbol,
            position_id=checkpoint.position_id,
            stop_order_id=checkpoint.stop_order_id,
            old_stop=checkpoint.stop_price,
            proposed_stop=proposed.stop_price,
            best_profit_pct=proposed.best_profit_pct,
            armed=proposed.active,
        ))
        self.assertEqual(state, "PREPARED")
        await amendments.mark_submitting(amendment)
        updated_stops = copy.deepcopy(old_stops)
        updated_stops[0]["stopLossPrice"] = D("1.73")
        pipeline = SharedTrailingPersistencePipeline(
            StopUpdatePersistencePipeline(
                amendments, CheckpointPersistenceCoordinator(
                    PostgresCheckpointJournal(self.pool),
                ),
            ),
        )
        inputs = dict(
            pre_witness=before, checkpoint=checkpoint,
            amendment=amendment,
            before_positions=copy.deepcopy(positions),
            before_stops=old_stops,
            after_positions_first=copy.deepcopy(positions),
            after_positions_second=copy.deepcopy(positions),
            after_stops_first=copy.deepcopy(updated_stops),
            after_stops_second=copy.deepcopy(updated_stops),
            after_inventory_first=counts(3, 1),
            after_inventory_second=counts(3, 1),
            observed_low=D("1.72"), price_tick=D("0.01"),
        )
        return pipeline, inputs, positions, updated_stops, order

    async def test_real_postgres_shared_trailing_checkpoint_and_restart(self):
        pipeline, inputs, positions, stops, order = (
            await self._initialized_shared_stop_test()
        )
        before_manual = copy.deepcopy((positions[:2], self.manual_stops))
        result = await pipeline.reconcile_bookended_and_persist(**inputs)
        self.assertEqual(result.checkpoint.checkpoint_version, 2)
        self.assertEqual(result.checkpoint.stop_price, D("1.73"))
        self.assertTrue(result.checkpoint.armed)
        self.assertFalse(result.live_authorization)
        self.assertEqual((positions[:2], self.manual_stops), before_manual)
        async with self.read_pool.acquire() as db:
            self.assertEqual(await db.fetchval("""
                SELECT state FROM public.t100_live_stop_amendments
            """), "VERIFIED")
            self.assertEqual(await db.fetchval("""
                SELECT count(*) FROM public.t100_live_position_checkpoint_revisions
            """), 2)
            self.assertEqual(await db.fetchval("""
                SELECT checkpoint_version
                FROM public.t100_live_position_checkpoints
            """), 2)
        fresh = await FourSlotSnapshotCoordinator(
            ledger=PostgresFourSlotLedgerReader(self.read_pool),
            venue=Venue([order], positions, stops),
            completeness=Completeness(counts(3, 1)),
        ).inspect(shared_manual_observation=True)
        self.assertEqual(
            fresh.status,
            "SHARED_ACCOUNT_MANAGED_PROVENANCE_MATCHED_NONAUTHORIZING",
        )
        self.assertEqual(fresh.manual_positions_observed, 2)
        self.assertEqual(fresh.occupied_slots, 1)

    async def test_real_postgres_shared_trailing_checkpoint_failure_then_recover(self):
        from app.t100_live_shared_trailing_persistence import (
            SharedTrailingPipelineHalted,
        )
        pipeline, inputs, _, _, _ = (
            await self._initialized_shared_stop_test()
        )
        async with self.pool.acquire() as db:
            await db.execute("""
                CREATE FUNCTION public.t100_ci_block_shared_stop_revision()
                RETURNS trigger LANGUAGE plpgsql AS $t100_shared_stop$
                BEGIN
                  IF NEW.checkpoint_version=2 THEN
                    RAISE EXCEPTION 'CI-only stop checkpoint rollback';
                  END IF;
                  RETURN NEW;
                END $t100_shared_stop$
            """)
            await db.execute("""
                CREATE TRIGGER t100_ci_shared_stop_revision_abort
                BEFORE INSERT ON public.t100_live_position_checkpoint_revisions
                FOR EACH ROW EXECUTE FUNCTION
                  public.t100_ci_block_shared_stop_revision()
            """)
        try:
            with self.assertRaisesRegex(
                SharedTrailingPipelineHalted,
                "CHECKPOINT_PERSISTENCE_FAILED",
            ):
                await pipeline.reconcile_bookended_and_persist(**inputs)
            async with self.read_pool.acquire() as db:
                self.assertEqual(await db.fetchval("""
                    SELECT checkpoint_version
                    FROM public.t100_live_position_checkpoints
                """), 1)
                self.assertEqual(await db.fetchval("""
                    SELECT count(*)
                    FROM public.t100_live_position_checkpoint_revisions
                """), 1)
                self.assertEqual(await db.fetchval("""
                    SELECT state FROM public.t100_live_stop_amendments
                """), "VERIFIED")
        finally:
            async with self.pool.acquire() as db:
                await db.execute("""
                    DROP TRIGGER t100_ci_shared_stop_revision_abort
                    ON public.t100_live_position_checkpoint_revisions
                """)
                await db.execute(
                    "DROP FUNCTION public.t100_ci_block_shared_stop_revision()"
                )
        resumed = await pipeline.reconcile_bookended_and_persist(
            **inputs, restart_reconciliation=True,
        )
        self.assertEqual(resumed.checkpoint.checkpoint_version, 2)
        self.assertFalse(resumed.exchange_mutation_sent_by_coordinator)
        async with self.read_pool.acquire() as db:
            self.assertEqual(await db.fetchval("""
                SELECT count(*)
                FROM public.t100_live_position_checkpoint_revisions
            """), 2)
            self.assertEqual(await db.fetchval("""
                SELECT count(*) FROM public.t100_live_stop_amendments
            """), 1)

    async def test_account_wide_four_slot_count_and_duplicate_protection(self):
        first, second = await asyncio.gather(
            self.reserve(signal_id=401, symbol="NEW_USDT"),
            self.reserve(signal_id=402, symbol="SECOND_USDT"),
        )
        self.assertEqual(
            {first.occupied_after_reservation, second.occupied_after_reservation},
            {3, 4},
        )
        with self.assertRaises(FourSlotReservationHalted):
            await self.reserve(signal_id=403, symbol="THIRD_USDT")
        with self.assertRaises(FourSlotReservationHalted):
            await self.reserve(signal_id=404, symbol="MANUALA_USDT")
        # Only the first staged order may progress while the other
        # reservation is unresolved; competing order attempts do not POST.
        intent, stage = await self.stage(first)
        self.assertEqual(stage.state, "SUBMITTING")
        with self.assertRaises(FourSlotAttemptHalted):
            await self.stage(second)
        self.assertEqual(
            (self.manual_positions, self.manual_stops), self.original_manual,
        )

    async def test_stale_leader_or_unknown_outcome_fail_closed(self):
        reserve = await self.reserve()
        async with self.pool.acquire() as db:
            await db.execute("""
                UPDATE public.t100_live_executor_leases
                SET fencing_token=12
                WHERE lease_name='t100-live-executor'
            """)
        with self.assertRaises(FourSlotAttemptHalted):
            await self.stage(reserve)
        self.assertEqual(
            (await self.snapshot_state(reserve.external_oid))["state"], None,
        )
        async with self.pool.acquire() as db:
            await db.execute("""
                UPDATE public.t100_live_executor_leases
                SET fencing_token=11
                WHERE lease_name='t100-live-executor'
            """)
        intent, stage = await self.stage(reserve)
        await self.journal.latch_unknown(
            signal_id=reserve.signal_id,
            external_oid=reserve.external_oid, lease=lease(),
        )
        self.assertEqual(
            (await self.snapshot_state(reserve.external_oid))["state"],
            "UNKNOWN",
        )
        with self.assertRaises(FourSlotAttemptHalted):
            await self.stage(reserve)
        batch, _ = self.prepared(intent, stage)
        with self.assertRaises(ProtectedCommitHalted):
            await self.commit_store.persist(
                batch=batch, pre_attempt_witness=self.pre,
                lease=lease(), observed_at=self.observed_at,
                shared_pre_positions=self.manual_positions,
                shared_pre_stops=self.manual_stops,
            )
        self.assertEqual(
            (await self.snapshot_state(reserve.external_oid))["state"],
            "UNKNOWN",
        )


if __name__ == "__main__":
    require_isolated_ci()
    unittest.main()
