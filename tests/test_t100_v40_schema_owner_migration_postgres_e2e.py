"""Real PostgreSQL 16/pg_dump e2e for V40 owner CLI, disposable loopback only.

Never uses Render credentials, MEXC, network addresses other than 127.0.0.1,
or production trading flags. All data disappears with the CI service.
"""
import os
from pathlib import Path
import subprocess

import asyncpg
import pytest

from app import t100_v40_schema_owner_migration_cli as runner


BASE=(
    "027_t100_live_order_intents.sql",
    "028_t100_live_vol30_h4.sql",
    "029_t100_live_durable_state.sql",
    "030_t100_live_executor_lease.sql",
    "031_t100_live_v40_four_slot_reservations.sql",
    "032_t100_live_v40_dispatch_claims.sql",
)


def require_ci():
    env=os.environ
    assert env.get("CI")=="true"
    assert env.get("T100_V40_SCHEMA_MIGRATION_CI_ONLY")=="yes"
    assert env.get("PGHOST")=="127.0.0.1"
    assert env.get("PGPORT")=="5432"
    assert env.get("PGDATABASE")=="mexc_exhaustion"
    assert env.get("PGUSER")=="postgres"
    assert env.get("PGPASSWORD")=="t100-ci-only-local-postgres"
    assert env.get("T100_V40_SCHEMA_OWNER_DSN")==(
        "postgresql://postgres:t100-ci-only-local-postgres"
        "@127.0.0.1:5432/mexc_exhaustion"
    )
    assert not env.get("MEXC_API_KEY")
    assert not env.get("MEXC_API_SECRET")
    assert not env.get("DATABASE_URL")
    assert runner.disabled_local_controls(env)


@pytest.mark.asyncio
async def test_owner_cli_applies_all_three_atomically_and_reapply_is_readonly(tmp_path):
    require_ci()
    conn=await asyncpg.connect(
        dsn=os.environ["T100_V40_SCHEMA_OWNER_DSN"],
        ssl="disable",timeout=5,
    )
    try:
        await conn.execute("CREATE ROLE t100_live_executor_runtime NOLOGIN")
        await conn.execute(
            "GRANT USAGE ON SCHEMA public TO t100_live_executor_runtime"
        )
        for name in BASE:
            await conn.execute(
                (runner.ROOT/"migrations"/name).read_text()
            )
    finally:
        await conn.close()

    backup=tmp_path/"mexc_exhaustion_ci_before_042_044.dump"
    subprocess.run([
        "pg_dump","-Fc","-f",str(backup),"mexc_exhaustion",
    ],check=True,timeout=25,
       env={**os.environ,"PGPASSWORD":"t100-ci-only-local-postgres"})
    assert backup.stat().st_size>1024
    inspection=await runner.run_once(
        apply=False,backup_file=None,env=dict(os.environ),
    )
    assert inspection=="V40_MIGRATION_SCHEMA_INCOMPLETE_INSPECTION_ONLY"

    applied=await runner.run_once(
        apply=True,backup_file=backup,env=dict(os.environ),
    )
    assert applied=="V40_MIGRATION_042_044_COMMITTED_EXECUTION_STILL_DISABLED"
    repeated=await runner.run_once(
        apply=True,backup_file=backup,env=dict(os.environ),
    )
    assert repeated=="V40_MIGRATION_ALREADY_APPLIED_EXACT_GRANTS_VERIFIED"

    verified=await runner.run_once(
        apply=False,backup_file=None,env=dict(os.environ),
    )
    assert verified=="V40_MIGRATION_ALREADY_PRESENT_INSPECTION_ONLY"

    check=await asyncpg.connect(
        dsn=os.environ["T100_V40_SCHEMA_OWNER_DSN"],
        ssl="disable",timeout=5,
    )
    try:
        assert all(await runner.catalog(check))
        await runner.verify_grants(check)
        assert await check.fetchval(
            """SELECT COUNT(*)::int
               FROM public.t100_live_v40_entry_contract_specs"""
        )==0
        assert await check.fetchval(
            """SELECT COUNT(*)::int
               FROM public.t100_v40_exit_bar_cursor"""
        )==0
    finally:
        await check.close()
