"""Opt-in, read-only runtime proof of V40's assembled first-position path.

This checks the actual Render PostgreSQL logins and constructs the ONE-SHOT
coordinator with a deliberately disabled/no-POST order transport. It never
acquires a leader lease, reserves a slot, modifies a journal, submits an order,
or certifies the exchange SL75 lifecycle.

CLI usage, only on the dedicated V40 worker:
    python -m app.t100_v40_render_executor_assembly_preflight --once
Requires T100_V40_EXECUTOR_ASSEMBLY_GET_ONLY_ENABLED=true and all existing
paper/kill-latched monitor guards. No credentials, raw responses, SQL
connection strings, symbols or order IDs are printed.
"""
from __future__ import annotations

import argparse
import asyncio
from dataclasses import dataclass, asdict
import json
import os
from typing import Any, Mapping

from app.t100_live_db_inspect import EXPECTED_DATABASE
from app.t100_live_role_preflight import TARGET_ROLE
from app.t100_live_v40_stage2_source import RESTRICTED_V40_SIGNAL_READER_ROLE
from app.t100_v40_render_supervised_pilot_worker import monitor_environment
from app.t100_v40_executor_assembly import (
    V40AssemblyHalted, V40FirstPilotGetOnly, DisabledV40OrderSink,
    assemble_v40_first_pilot,
)


FLAG="T100_V40_EXECUTOR_ASSEMBLY_GET_ONLY_ENABLED"


@dataclass(frozen=True)
class V40AssemblyPreflight:
    status: str
    separate_database_roles_verified: bool=False
    source_role_readonly_verified: bool=False
    executor_role_readonly_verified: bool=False
    coordinator_wired: bool=False
    strategy_source_wired: bool=False
    exact_market_depth_wired: bool=False
    dispatch_claim_wired: bool=False
    protected_commit_wired: bool=False
    initial_pilot_flat_only: bool=False
    native_sl75_execution_certified: bool=False
    independently_running_guardian_certified: bool=False
    cross_margin_risk_certified: bool=False
    live_post_adapter_connected: bool=False
    exchange_posts: int=0
    database_writes: int=0
    entry_allowed: bool=False
    live_authorization: bool=False


def _safe_env(env:Mapping[str,str]) -> bool:
    return (
        monitor_environment(env)
        and env.get(FLAG)=="true"
        and env.get("T100_V40_EXECUTOR_DISABLED_WORKER")=="true"
        and env.get("T100_LIVE_KILL_SWITCH")=="true"
        and env.get("T100_LIVE_ORDER_API_ENABLED")=="false"
        and env.get("T100_EXECUTION_MODE")=="paper"
        and env.get("EXECUTION_ENABLED")=="false"
        and env.get("TRADING_MODE")=="paper"
        and not env.get("DATABASE_URL")
        and not env.get("T100_V40_TRADER_API_KEY")
        and not env.get("T100_V40_TRADER_API_SECRET")
    )


async def _role_readonly(pool:Any,expected_role:str) -> bool:
    try:
        async with pool.acquire() as conn:
            async with conn.transaction(
                isolation="repeatable_read",readonly=True,
            ):
                return (
                    await conn.fetchval("SELECT current_user")==expected_role
                    and await conn.fetchval("SELECT current_database()")==EXPECTED_DATABASE
                    and await conn.fetchval(
                        "SELECT current_setting('transaction_isolation')"
                    )=="repeatable read"
                    and await conn.fetchval(
                        "SELECT current_setting('transaction_read_only')"
                    )=="on"
                )
    except Exception:
        return False


async def inspect_v40_assembly(
    *,signal_pool:Any,runtime_pool:Any,signed_get:V40FirstPilotGetOnly,
    public_get:Any,
) -> V40AssemblyPreflight:
    """Injectable, no-mutation composition and two independent SQL SELECTs."""
    halted=lambda status:V40AssemblyPreflight(status=status)
    if signal_pool is runtime_pool:
        return halted("V40_EXECUTOR_ASSEMBLY_ROLES_NOT_SEPARATE_HALT")
    first=await _role_readonly(
        signal_pool,RESTRICTED_V40_SIGNAL_READER_ROLE,
    )
    second=await _role_readonly(runtime_pool,TARGET_ROLE)
    if not first or not second:
        return halted("V40_EXECUTOR_ASSEMBLY_ROLE_READONLY_CHECK_HALT")
    try:
        assembled=assemble_v40_first_pilot(
            signal_reader_pool=signal_pool,
            executor_runtime_pool=runtime_pool,
            signed_get=signed_get,
            public_get=public_get,
        )
        c=assembled.coordinator
        from app.t100_live_v40_stage2_source import PostgresV40Stage2Source
        from app.t100_live_v40_candidate_market_witness import MexcV40CandidateMarketWitness
        from app.t100_live_four_slot_dispatch_claims import PostgresFourSlotDispatchClaims
        from app.t100_live_v40_protected_commit import PostgresProtectedV40Commit
        if not (
            type(c.source) is PostgresV40Stage2Source
            and c.source.restricted_source_reader is True
            and c.source.pool is signal_pool
            and type(c.witness) is MexcV40CandidateMarketWitness
            and c.depth is c.witness
            and type(c.claims) is PostgresFourSlotDispatchClaims
            and type(c.commit_writer) is PostgresProtectedV40Commit
            and type(c.transport) is DisabledV40OrderSink
            and c.initial_pilot_only is True
        ):
            return halted("V40_EXECUTOR_ASSEMBLY_PROVENANCE_MISMATCH_HALT")
    except Exception:
        return halted("V40_EXECUTOR_ASSEMBLY_DEPENDENCY_INJECTION_HALT")
    return V40AssemblyPreflight(
        status="V40_EXECUTOR_ASSEMBLED_TWO_ROLES_GET_ONLY_NO_LIVE_POST",
        separate_database_roles_verified=True,
        source_role_readonly_verified=True,
        executor_role_readonly_verified=True,
        coordinator_wired=True,
        strategy_source_wired=True,
        exact_market_depth_wired=True,
        dispatch_claim_wired=True,
        protected_commit_wired=True,
        initial_pilot_flat_only=True,
    )


async def observe_v40_assembly_render(
    env:Mapping[str,str],
) -> V40AssemblyPreflight:
    """No production exchange mutation capability; no journal writes."""
    if not _safe_env(env):
        return V40AssemblyPreflight(
            "V40_EXECUTOR_ASSEMBLY_PAPER_KILL_OR_SCOPE_HALT"
        )
    source_dsn=env.get("T100_V40_SIGNAL_READER_DSN","")
    runtime_dsn=env.get("T100_V40_EXECUTOR_RUNTIME_DSN","")
    key=env.get("MEXC_API_KEY","")
    secret=env.get("MEXC_API_SECRET","")
    if (
        type(source_dsn) is not str or not source_dsn.startswith(
            ("postgresql://","postgres://")
        )
        or type(runtime_dsn) is not str or not runtime_dsn.startswith(
            ("postgresql://","postgres://")
        )
        or type(key) is not str or len(key)<12 or key!=key.strip()
        or type(secret) is not str or len(secret)<16 or secret!=secret.strip()
    ):
        return V40AssemblyPreflight("V40_EXECUTOR_ASSEMBLY_CONNECTION_INPUTS_HALT")

    import asyncpg
    import httpx

    signal_pool=None
    runtime_pool=None
    try:
        signal_pool=await asyncpg.create_pool(
            dsn=source_dsn,ssl="require",min_size=1,max_size=1,
            timeout=9,command_timeout=12,
            server_settings={"default_transaction_read_only":"on"},
        )
        runtime_pool=await asyncpg.create_pool(
            dsn=runtime_dsn,ssl="require",min_size=1,max_size=1,
            timeout=9,command_timeout=12,
            server_settings={"default_transaction_read_only":"on"},
        )
        async with httpx.AsyncClient(
            base_url="https://api.mexc.com",timeout=8,
            trust_env=False,follow_redirects=False,
        ) as http:
            # Both public and private sources have no POST interface;
            # constructors do NOT issue any signed MEXC GET.
            get_only=V40FirstPilotGetOnly(
                http,api_key=key,api_secret=secret,
            )
            return await inspect_v40_assembly(
                signal_pool=signal_pool,runtime_pool=runtime_pool,
                signed_get=get_only,public_get=http,
            )
    except Exception:
        return V40AssemblyPreflight(
            "V40_EXECUTOR_ASSEMBLY_CONNECTION_OR_CONTEXT_FAILED_HALT"
        )
    finally:
        if runtime_pool is not None:
            await runtime_pool.close()
        if signal_pool is not None:
            await signal_pool.close()


def main()->int:
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--once",action="store_true",required=True)
    parser.parse_args()
    try:
        report=asyncio.run(observe_v40_assembly_render(dict(os.environ)))
    except Exception:
        report=V40AssemblyPreflight("V40_EXECUTOR_ASSEMBLY_UNEXPECTED_HALT")
    print(json.dumps(asdict(report),sort_keys=True),flush=True)
    return 0 if report.coordinator_wired else 2


if __name__=="__main__":
    raise SystemExit(main())
