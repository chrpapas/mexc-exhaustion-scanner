"""Explicit operator-run, fail-closed V40 migrations 042–044.

NEVER imported by a Render worker. No defaults can connect to production.
Only an explicit schema-owner connection (environment variable), a verified
backup file, hard-disabled local execution controls, and an inactive ledger
permit a one-transaction DDL application. This grants no trading permission.

Usage:
  T100_V40_SCHEMA_OWNER_DSN=... python -m app.t100_v40_schema_owner_migration_cli --inspect
  T100_V40_SCHEMA_OWNER_DSN=... python -m app.t100_v40_schema_owner_migration_cli --apply --backup-file /path/to/verified-backup.dump

Do not put credentials in CLI flags, code, tickets, logs or chat.
"""
from __future__ import annotations

import argparse
import asyncio
from pathlib import Path
import os
import subprocess
from typing import Any, Mapping

from app.t100_live_db_inspect import EXPECTED_DATABASE

MIGRATIONS=(
    "042_v40_exit_bar_cursor_UNRELEASED.sql",
    "043_v40_immutable_bar_observed_UNRELEASED.sql",
    "044_v40_immutable_entry_contract_specs_UNRELEASED.sql",
)
REQUIRED_BASE_TABLES=(
    "t100_live_position_checkpoints",
    "t100_live_position_checkpoint_revisions",
    "t100_live_v40_slot_reservations",
    "t100_live_v40_dispatch_claims",
    "t100_live_order_intents",
    "t100_live_stop_amendments",
    "t100_live_final_position_accounting",
    "t100_live_executor_leases",
)
EXPECTED_ROLE="t100_live_executor_runtime"
ROOT=Path(__file__).resolve().parents[1]


class V40OwnerMigrationHalted(RuntimeError):
    """Never includes the password, DSN, SQL error or exchange data."""


def strip_outer_transaction(source:str)->str:
    """Refuse changes to reviewed migration wrapper format."""
    lines=source.splitlines()
    tags=[(i,s.strip().upper()) for i,s in enumerate(lines)
          if s.strip().upper() in ("BEGIN;","COMMIT;")]
    if len(tags)!=2 or [x[1] for x in tags]!=["BEGIN;","COMMIT;"]:
        raise V40OwnerMigrationHalted("V40_MIGRATION_WRAPPER_UNEXPECTED")
    before,after=tags[0][0],tags[1][0]
    if any(line.strip() and not line.lstrip().startswith("--")
           for line in lines[after+1:]):
        raise V40OwnerMigrationHalted("V40_MIGRATION_AFTER_COMMIT_UNEXPECTED")
    if any(line.lstrip().startswith(("\\i","\\!","\\copy"))
           for line in lines):
        raise V40OwnerMigrationHalted("V40_MIGRATION_PSQL_META_FORBIDDEN")
    return "\n".join(lines[:before]+lines[before+1:after])+"\n"


def disabled_local_controls(env:Mapping[str,str])->bool:
    return all((
        env.get("T100_EXECUTION_MODE")=="paper",
        env.get("T100_LIVE_ORDER_API_ENABLED")=="false",
        env.get("T100_LIVE_KILL_SWITCH")=="true",
        env.get("EXECUTION_ENABLED")=="false",
        env.get("TRADING_MODE")=="paper",
    ))


async def catalog(conn:Any)->tuple[bool,bool,bool,bool]:
    return (
        await conn.fetchval(
            "SELECT to_regclass('public.t100_v40_exit_bar_cursor') IS NOT NULL"
        ),
        await conn.fetchval(
            "SELECT to_regclass('public.t100_live_v40_entry_contract_specs') IS NOT NULL"
        ),
        await conn.fetchval(
            """SELECT EXISTS (SELECT 1 FROM pg_constraint
               WHERE conrelid=to_regclass('public.t100_live_position_checkpoints')
                 AND pg_get_constraintdef(oid) LIKE '%BAR_OBSERVED%')"""
        ),
        await conn.fetchval(
            """SELECT EXISTS (SELECT 1 FROM pg_constraint
               WHERE conrelid=to_regclass('public.t100_live_position_checkpoint_revisions')
                 AND pg_get_constraintdef(oid) LIKE '%BAR_OBSERVED%')"""
        ),
    )


async def require_owner_and_flat(conn:Any)->None:
    if (
        await conn.fetchval("SELECT current_database()")!=EXPECTED_DATABASE
        or await conn.fetchval("SELECT current_setting('transaction_read_only')")!="off"
        or await conn.fetchval("SELECT pg_is_in_recovery()") is not False
        or await conn.fetchval("SELECT current_user")==EXPECTED_ROLE
        or await conn.fetchval(
            "SELECT has_schema_privilege(current_user,'public','CREATE')"
        ) is not True
        or await conn.fetchval(
            "SELECT to_regrole('t100_live_executor_runtime') IS NOT NULL"
        ) is not True
    ):
        raise V40OwnerMigrationHalted("V40_MIGRATION_DB_OWNER_OR_ROLE_UNVERIFIED")
    for table in REQUIRED_BASE_TABLES:
        if await conn.fetchval(
            "SELECT to_regclass($1) IS NOT NULL", "public."+table,
        ) is not True:
            raise V40OwnerMigrationHalted("V40_MIGRATION_BASE_SCHEMA_MISSING")
    for sql in (
        "SELECT count(*)::int FROM public.t100_live_position_checkpoints WHERE lifecycle_state<>'CLOSED'",
        "SELECT count(*)::int FROM public.t100_live_v40_slot_reservations WHERE state<>'RELEASED'",
        "SELECT count(*)::int FROM public.t100_live_order_intents WHERE state IN ('PREPARED','SUBMITTING','UNKNOWN','REVIEW')",
        "SELECT count(*)::int FROM public.t100_live_stop_amendments WHERE state<>'VERIFIED'",
        "SELECT count(*)::int FROM public.t100_live_executor_leases WHERE lease_expires_at>clock_timestamp()",
    ):
        if await conn.fetchval(sql)!=0:
            raise V40OwnerMigrationHalted("V40_MIGRATION_OPEN_OR_UNRESOLVED_LEDGER_BLOCK")
    

async def verify_grants(conn:Any)->None:
    query="""SELECT
      has_table_privilege($1,'public.t100_v40_exit_bar_cursor','SELECT') AS cs,
      has_table_privilege($1,'public.t100_v40_exit_bar_cursor','INSERT') AS ci,
      has_table_privilege($1,'public.t100_v40_exit_bar_cursor','DELETE') AS cd,
      has_column_privilege($1,'public.t100_v40_exit_bar_cursor',
                           'pending_open_ts','UPDATE') AS cu,
      has_column_privilege($1,'public.t100_v40_exit_bar_cursor',
                           'entry_external_oid','UPDATE') AS cr,
      has_table_privilege($1,'public.t100_live_v40_entry_contract_specs','SELECT') AS es,
      has_table_privilege($1,'public.t100_live_v40_entry_contract_specs','INSERT') AS ei,
      has_table_privilege($1,'public.t100_live_v40_entry_contract_specs','UPDATE') AS eu,
      has_table_privilege($1,'public.t100_live_v40_entry_contract_specs','DELETE') AS ed"""
    expected=dict(cs=True,ci=True,cd=False,cu=True,cr=False,
                  es=True,ei=True,eu=False,ed=False)
    row=await conn.fetchrow(query,EXPECTED_ROLE)
    if row is None or dict(row)!=expected:
        raise V40OwnerMigrationHalted("V40_MIGRATION_RUNTIME_GRANTS_UNVERIFIED")


async def run_once(*,apply:bool,backup_file:Path|None,env:Mapping[str,str])->str:
    if not disabled_local_controls(env):
        raise V40OwnerMigrationHalted("V40_MIGRATION_LOCAL_TRADING_CONTROLS_NOT_DISABLED")
    dsn=env.get("T100_V40_SCHEMA_OWNER_DSN")
    if not isinstance(dsn,str) or not dsn.startswith(("postgresql://","postgres://")):
        raise V40OwnerMigrationHalted("V40_MIGRATION_OWNER_DSN_MISSING")
    if apply and (
        backup_file is None
        or not backup_file.is_file()
        or backup_file.stat().st_size<1024
    ):
        raise V40OwnerMigrationHalted("V40_MIGRATION_BACKUP_FILE_REQUIRED")
    if apply:
        # A nonempty file is not proof of an actually restorable backup.
        # Require a valid PostgreSQL custom-format archive of the exact DB.
        try:
            verified=subprocess.run(
                ["pg_restore","--list",str(backup_file)],
                capture_output=True,text=True,timeout=20,check=True,
            )
            if "dbname: "+EXPECTED_DATABASE not in verified.stdout:
                raise ValueError("WRONG_BACKUP_DATABASE")
        except Exception:
            raise V40OwnerMigrationHalted(
                "V40_MIGRATION_RESTORABLE_DB_BACKUP_NOT_VERIFIED"
            ) from None
    # Import only after all offline guards, never pass DSN to logger.
    import asyncpg
    conn=None
    try:
        conn=await asyncpg.connect(dsn=dsn,ssl="require",timeout=12,
                                   command_timeout=20)
        # Read-only inspection never invokes a DDL statement.
        if not apply:
            async with conn.transaction(readonly=True):
                # A live connection must be deliberately verified by owner
                # before an apply attempt. Inspection is non-authorizing.
                if await conn.fetchval("SELECT current_database()")!=EXPECTED_DATABASE:
                    raise V40OwnerMigrationHalted("V40_MIGRATION_WRONG_DATABASE")
                states=await catalog(conn)
                return ("V40_MIGRATION_ALREADY_PRESENT_INSPECTION_ONLY"
                        if all(states) else
                        "V40_MIGRATION_SCHEMA_INCOMPLETE_INSPECTION_ONLY")
        scripts=[
            strip_outer_transaction((ROOT/"migrations"/name).read_text())
            for name in MIGRATIONS
        ]
        async with conn.transaction(isolation="serializable"):
            # Exactly the account-wide lock used by four-slot writers.
            await conn.execute("SELECT pg_advisory_xact_lock(10040,31)")
            await require_owner_and_flat(conn)
            states=await catalog(conn)
            if all(states):
                await verify_grants(conn)
                return "V40_MIGRATION_ALREADY_APPLIED_EXACT_GRANTS_VERIFIED"
            if any(states):
                raise V40OwnerMigrationHalted(
                    "V40_MIGRATION_PARTIALLY_APPLIED_MANUAL_RECONCILE_ONLY"
                )
            await conn.execute("SET LOCAL lock_timeout='5s'")
            await conn.execute("SET LOCAL statement_timeout='15s'")
            for sql in scripts:
                await conn.execute(sql)
            if not all(await catalog(conn)):
                raise V40OwnerMigrationHalted(
                    "V40_MIGRATION_POST_DDL_CATALOG_UNVERIFIED"
                )
            await verify_grants(conn)
        # Successful transaction exit is not live trading authorization.
        return "V40_MIGRATION_042_044_COMMITTED_EXECUTION_STILL_DISABLED"
    except V40OwnerMigrationHalted:
        raise
    except Exception:
        # The transaction may have committed before a lost ACK. Never retry
        # until an independent catalog read verifies exact status and grants.
        raise V40OwnerMigrationHalted(
            "V40_MIGRATION_SQL_OUTCOME_UNVERIFIED_REINSPECT_ONLY"
        ) from None
    finally:
        if conn is not None:
            await conn.close()


def main()->int:
    p=argparse.ArgumentParser(description="V40 schema-owner migration preflight")
    choose=p.add_mutually_exclusive_group(required=True)
    choose.add_argument("--inspect",action="store_true")
    choose.add_argument("--apply",action="store_true")
    p.add_argument("--backup-file",type=Path)
    args=p.parse_args()
    try:
        result=asyncio.run(run_once(
            apply=args.apply,backup_file=args.backup_file,env=dict(os.environ),
        ))
    except V40OwnerMigrationHalted as exc:
        print(str(exc),flush=True)
        return 2
    print(result,flush=True)
    return 0


if __name__=="__main__":
    raise SystemExit(main())
