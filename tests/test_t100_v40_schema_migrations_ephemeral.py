"""Isolated PG16 proof for unreleased V40 schema 042–044.

NEVER connects to Render, MEXC or an external database. The only accepted
database is the ephemeral 127.0.0.1 CI PostgreSQL service, with a fixed test
password, disabled order controls and no live credentials.
"""
import os
from pathlib import Path

import asyncpg
import pytest


ROOT=Path(__file__).resolve().parents[1]
MIGRATIONS=(
    "027_t100_live_order_intents.sql",
    "028_t100_live_vol30_h4.sql",
    "029_t100_live_durable_state.sql",
    "030_t100_live_executor_lease.sql",
    "031_t100_live_v40_four_slot_reservations.sql",
    "032_t100_live_v40_dispatch_claims.sql",
    "042_v40_exit_bar_cursor_UNRELEASED.sql",
    "043_v40_immutable_bar_observed_UNRELEASED.sql",
    "044_v40_immutable_entry_contract_specs_UNRELEASED.sql",
)
TEST_PASSWORD="t100-ci-only-local-postgres"


def require_isolated_ci():
    # The general offline suite has no disposable PostgreSQL service.
    # Only the dedicated workflow opts in to this deliberately destructive test.
    if os.getenv("T100_V40_PG_SCHEMA_CI_ONLY")!="yes":
        pytest.skip("disposable-PG16 schema workflow only")
    if not all((
        os.getenv("CI")=="true",
        os.getenv("T100_V40_PG_SCHEMA_CI_ONLY")=="yes",
        os.getenv("T100_EXECUTION_MODE")=="paper",
        os.getenv("T100_LIVE_ORDER_API_ENABLED")=="false",
        os.getenv("T100_LIVE_KILL_SWITCH")=="true",
        os.getenv("EXECUTION_ENABLED")=="false",
        os.getenv("PGHOST")=="127.0.0.1",
        os.getenv("PGPORT")=="5432",
        os.getenv("PGDATABASE")=="mexc_exhaustion",
        os.getenv("PGUSER")=="postgres",
        os.getenv("PGPASSWORD")==TEST_PASSWORD,
        not os.getenv("DATABASE_URL"),
        not os.getenv("T100_V40_EXECUTOR_RUNTIME_DSN"),
        not os.getenv("T100_V40_SIGNAL_READER_DSN"),
        not os.getenv("MEXC_API_KEY"),
        not os.getenv("MEXC_API_SECRET"),
        not os.getenv("T100_V40_TRADER_API_KEY"),
        not os.getenv("T100_V40_TRADER_API_SECRET"),
    )):
        raise RuntimeError("V40_SCHEMA_EPHEMERAL_PG16_ONLY")


@pytest.mark.asyncio
async def test_real_postgres_042_044_are_atomic_and_runtime_grants_minimal():
    require_isolated_ci()
    conn=await asyncpg.connect(
        host="127.0.0.1",port=5432,database="mexc_exhaustion",
        user="postgres",password=TEST_PASSWORD,ssl=False,timeout=5,
    )
    try:
        assert await conn.fetchval("SELECT current_database()")=="mexc_exhaustion"
        await conn.execute(
            "CREATE ROLE t100_live_executor_runtime NOLOGIN"
        )
        await conn.execute(
            "GRANT USAGE ON SCHEMA public TO t100_live_executor_runtime"
        )
        for name in MIGRATIONS:
            # Source-controlled production DDL is executed ONLY against
            # this disposable PostgreSQL service.
            await conn.execute((ROOT/"migrations"/name).read_text())

        assert await conn.fetchval(
            "SELECT to_regclass('public.t100_v40_exit_bar_cursor') IS NOT NULL"
        ) is True
        assert await conn.fetchval(
            "SELECT to_regclass('public.t100_live_v40_entry_contract_specs') IS NOT NULL"
        ) is True
        for table,column in (
            ("t100_live_position_checkpoints","last_write_reason"),
            ("t100_live_position_checkpoint_revisions","write_reason"),
        ):
            assert await conn.fetchval(
                """SELECT EXISTS (
                    SELECT 1 FROM pg_constraint c
                    WHERE c.conrelid=to_regclass($1)
                      AND pg_get_constraintdef(c.oid) LIKE '%BAR_OBSERVED%'
                )""",
                f"public.{table}",
            ),(table,column)

        permitted=await conn.fetchrow(
            """SELECT
              has_table_privilege('t100_live_executor_runtime',
                'public.t100_v40_exit_bar_cursor','SELECT') AS cursor_select,
              has_table_privilege('t100_live_executor_runtime',
                'public.t100_v40_exit_bar_cursor','INSERT') AS cursor_insert,
              has_table_privilege('t100_live_executor_runtime',
                'public.t100_v40_exit_bar_cursor','DELETE') AS cursor_delete,
              has_column_privilege('t100_live_executor_runtime',
                'public.t100_v40_exit_bar_cursor',
                'pending_open_ts','UPDATE') AS cursor_stage,
              has_column_privilege('t100_live_executor_runtime',
                'public.t100_v40_exit_bar_cursor',
                'entry_external_oid','UPDATE') AS cursor_origin_change,
              has_table_privilege('t100_live_executor_runtime',
                'public.t100_live_v40_entry_contract_specs','SELECT') AS entry_read,
              has_table_privilege('t100_live_executor_runtime',
                'public.t100_live_v40_entry_contract_specs','INSERT') AS entry_write,
              has_table_privilege('t100_live_executor_runtime',
                'public.t100_live_v40_entry_contract_specs','UPDATE') AS entry_update,
              has_table_privilege('t100_live_executor_runtime',
                'public.t100_live_v40_entry_contract_specs','DELETE') AS entry_delete"""
        )
        assert dict(permitted)==dict(
            cursor_select=True,cursor_insert=True,cursor_delete=False,
            cursor_stage=True,cursor_origin_change=False,
            entry_read=True,entry_write=True,
            entry_update=False,entry_delete=False,
        )

        # Exactly 75%-above-entry short stop with valid lot must pass.
        await conn.execute(
            """INSERT INTO public.t100_live_v40_slot_reservations (
                signal_id,symbol,external_oid,modeled_notional_usdt,
                ledger_sha256,inventory_sha256,snapshot_observed_at,
                holder_id,fencing_token,state
              ) VALUES (
                987,'SIREN_USDT','t100v1e987',200,
                repeat('a',64),repeat('b',64),clock_timestamp(),
                'ci-only',1,'SUBMITTING'
              )"""
        )
        await conn.execute(
            """INSERT INTO public.t100_live_v40_entry_contract_specs (
                signal_id,external_oid,symbol,contract_size,volume_step,
                price_tick,reference_price,notional_usdt,volume_contracts,
                stop_price,ledger_sha256,inventory_sha256
              ) VALUES (
                987,'t100v1e987','SIREN_USDT',1,1,1,100,200,2,175,
                repeat('a',64),repeat('b',64)
              )"""
        )

        # The exact original contract spec must remain immutable, even
        # if a higher-privilege session attempts to rewrite/delete it.
        for statement in (
            "UPDATE public.t100_live_v40_entry_contract_specs SET contract_size=2",
            "DELETE FROM public.t100_live_v40_entry_contract_specs",
        ):
            with pytest.raises(asyncpg.RaiseError):
                await conn.execute(statement)

        # Bad SL75 and notional cannot slip through constraints.
        for statement in (
            """INSERT INTO public.t100_live_v40_entry_contract_specs
               (signal_id,external_oid,symbol,contract_size,volume_step,
                price_tick,reference_price,notional_usdt,volume_contracts,
                stop_price,ledger_sha256,inventory_sha256)
               VALUES (988,'t100v1e988','SIREN_USDT',1,1,1,100,200,2,174,
                       repeat('a',64),repeat('b',64))""",
            """INSERT INTO public.t100_live_v40_entry_contract_specs
               (signal_id,external_oid,symbol,contract_size,volume_step,
                price_tick,reference_price,notional_usdt,volume_contracts,
                stop_price,ledger_sha256,inventory_sha256)
               VALUES (988,'t100v1e988','SIREN_USDT',1,1,1,100,201,2,175,
                       repeat('a',64),repeat('b',64))""",
        ):
            with pytest.raises(asyncpg.CheckViolationError):
                await conn.execute(statement)

        # A cursor can be staged, but it cannot be destroyed.
        await conn.execute(
            """INSERT INTO public.t100_v40_exit_bar_cursor(
                 position_id,entry_external_oid,signed_fill_at_ms,
                 checkpoint_version,stop_order_id,confirmed_stop_price,
                 best_profit_pct,trail_armed
               ) VALUES ('11','t100v1e987',1000,1,'99',175,0,false)"""
        )
        with pytest.raises(asyncpg.RaiseError):
            await conn.execute(
                "DELETE FROM public.t100_v40_exit_bar_cursor WHERE position_id='11'"
            )
    finally:
        await conn.close()
