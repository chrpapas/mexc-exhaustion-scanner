"""Real PG16 catalog ownership audit, only a disposable loopback CI DB."""
import os
import asyncpg
import pytest
from pathlib import Path

from app.t100_v40_schema_owner_role_probe import observe_owner_role_catalog

ROOT=Path(__file__).resolve().parents[1]
BASE=(
    "027_t100_live_order_intents.sql",
    "028_t100_live_vol30_h4.sql",
    "029_t100_live_durable_state.sql",
    "030_t100_live_executor_lease.sql",
    "031_t100_live_v40_four_slot_reservations.sql",
    "032_t100_live_v40_dispatch_claims.sql",
)


@pytest.mark.asyncio
async def test_owner_catalog_against_real_readonly_postgres():
    env=os.environ
    for key,value in (
        ("CI","true"),("T100_V40_OWNER_AUDIT_CI_ONLY","yes"),
        ("PGHOST","127.0.0.1"),("PGPORT","5432"),
        ("PGUSER","postgres"),("PGDATABASE","mexc_exhaustion"),
        ("PGPASSWORD","t100-ci-only-local-postgres"),
        ("T100_LIVE_KILL_SWITCH","true"),
        ("T100_LIVE_ORDER_API_ENABLED","false"),
        ("T100_EXECUTION_MODE","paper"),
    ):
        assert env.get(key)==value
    assert not env.get("MEXC_API_KEY")
    assert not env.get("MEXC_API_SECRET")
    assert not env.get("DATABASE_URL")
    conn=await asyncpg.connect(
        host="127.0.0.1",port=5432,database="mexc_exhaustion",
        user="postgres",password="t100-ci-only-local-postgres",
        ssl=False,timeout=5,
    )
    class Pool:
        def acquire(self):return Context(conn)
    class Context:
        def __init__(self,conn):self.conn=conn
        async def __aenter__(self):return self.conn
        async def __aexit__(self,*args):return False
    try:
        for name in BASE:
            await conn.execute((ROOT/"migrations"/name).read_text())
        no_owner=await observe_owner_role_catalog(Pool())
        assert no_owner.catalog_observed
        assert no_owner.candidate_role_exists is False
        assert no_owner.owner_role_permissions_sufficient_on_catalog is False

        await conn.execute("CREATE ROLE mexc_scanner NOLOGIN")
        await conn.execute("CREATE ROLE t100_live_executor_runtime NOLOGIN")
        await conn.execute("GRANT CREATE, USAGE ON SCHEMA public TO mexc_scanner")
        await conn.execute(
            "GRANT REFERENCES ON public.t100_live_v40_slot_reservations TO mexc_scanner"
        )
        await conn.execute(
            "ALTER TABLE public.t100_live_position_checkpoints OWNER TO mexc_scanner"
        )
        # First table alone is not sufficient.
        incomplete=await observe_owner_role_catalog(Pool())
        assert not incomplete.owner_role_permissions_sufficient_on_catalog
        assert incomplete.checkpoint_table_owner=="mexc_scanner"
        assert incomplete.revision_table_owner=="postgres"

        await conn.execute(
            "ALTER TABLE public.t100_live_position_checkpoint_revisions OWNER TO mexc_scanner"
        )
        complete=await observe_owner_role_catalog(Pool())
        assert complete.catalog_observed
        assert complete.owner_role_permissions_sufficient_on_catalog
        assert complete.candidate_can_reference_slot_reservations
        assert complete.candidate_can_create_public
        assert complete.runtime_role_exists
        assert not complete.authenticated_as_owner
        assert not complete.production_ddl_applied
        assert not complete.live_authorization
    finally:
        await conn.close()
