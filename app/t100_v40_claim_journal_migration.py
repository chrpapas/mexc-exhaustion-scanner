"""Explicit operator-reviewed, CREATE-ONLY V40 one-shot journal migrations.

Migrations 031+032 are NOT executed at import, worker startup, or normal
database code. --plan inspects named schema objects in one READ ONLY
transaction. --apply requires independently verified DB backup, private
owner DSN, exact typed operator acknowledgment, preflight of empty V40
namespace and pinned Git-blob hashes. Apply both SQL files in ONE
transaction with an advisory lock; a conflict or unknown state ROLLS BACK.

This does NOT authorize orders, create runtime grants, access MEXC, or
alter scanner/BOXWAVE/old-shadow data.
"""
from __future__ import annotations

import argparse
import asyncio
import getpass
import hashlib
import json
from pathlib import Path
from typing import Any, Mapping

from app.t100_live_db_inspect import EXPECTED_DATABASE

ROOT=Path(__file__).resolve().parents[1]
MIGRATIONS=(
    ("migrations/031_t100_live_v40_four_slot_reservations.sql",
     "ad35b3d05960f52611a707c3c499c2db5512eb5d"),
    ("migrations/032_t100_live_v40_dispatch_claims.sql",
     "3f937ddb03b3a9b827647401d73d57be49c49fb0"),
)
BASE_TABLES=(
    "public.t100_live_order_intents",
    "public.t100_live_position_checkpoints",
    "public.t100_live_executor_leases",
)
TARGET_OBJECTS=(
    "public.t100_live_v40_slot_reservations",
    "public.t100_live_v40_dispatch_claims",
    "public.t100_live_v40_one_active_symbol",
    "public.t100_live_v40_pending_resolution",
)
FUNCTION="public.t100_v40_reject_dispatch_claim_mutation()"
TRIGGER="t100_v40_dispatch_claim_immutable"
CONFIRM="CREATE ONLY T100 V40 JOURNAL 031 032"


class V40JournalMigrationBlocked(RuntimeError):
    """Fixed reason codes only, no DB strings or credentials."""


def pinned_migrations(root: Path=ROOT)->tuple[bytes,...]:
    items=[]
    for filename,expected_git_sha in MIGRATIONS:
        blob=(root/filename).read_bytes()
        digest=hashlib.sha1(
            b"blob "+str(len(blob)).encode()+b"\x00"+blob
        ).hexdigest()
        if digest!=expected_git_sha:
            raise V40JournalMigrationBlocked("V40_JOURNAL_PINNED_MIGRATION_BLOB_CHANGED")
        if b"CREATE " not in blob:
            raise V40JournalMigrationBlocked("V40_JOURNAL_MIGRATION_NOT_CREATE_ONLY")
        items.append(blob)
    return tuple(items)


async def plan(conn:Any)->dict[str,Any]:
    if await conn.fetchval("SELECT current_database()")!=EXPECTED_DATABASE:
        raise V40JournalMigrationBlocked("V40_JOURNAL_WRONG_DATABASE")
    user=await conn.fetchrow(
        """SELECT r.rolsuper AS superuser, current_user AS username
           FROM pg_catalog.pg_roles r WHERE r.rolname=current_user"""
    )
    if user is None:
        raise V40JournalMigrationBlocked("V40_JOURNAL_CURRENT_USER_UNVERIFIED")
    all_base=all(
        (await conn.fetchval("SELECT to_regclass($1)",table)) is not None
        for table in BASE_TABLES
    )
    none_new=all(
        (await conn.fetchval("SELECT to_regclass($1)",object_name)) is None
        for object_name in TARGET_OBJECTS
    )
    function_absent=await conn.fetchval(
        "SELECT to_regprocedure($1)",FUNCTION
    ) is None
    # The two target tables MUST NOT exist in any partial state. Trigger
    # existence is checked separately for collision with an earlier test.
    trigger_absent=await conn.fetchval(
        """SELECT NOT EXISTS (SELECT 1 FROM pg_trigger
           WHERE tgname=$1 AND NOT tgisinternal)""",TRIGGER
    ) is True
    permitted=all(
        (await conn.fetchval(
            "SELECT has_schema_privilege(current_user,'public',$1)",
            privilege,
        )) is True
        for privilege in ("CREATE","USAGE")
    )
    can_apply=(all_base and none_new and function_absent
               and trigger_absent and permitted)
    return {
        "status":(
            "V40_JOURNAL_CREATE_ONLY_PLAN_PASSED"
            if can_apply else "V40_JOURNAL_SCHEMA_OR_OWNER_NOT_READY"
        ),
        "current_database_verified":True,
        "base_live_schema_present":all_base,
        "v40_namespace_empty":bool(none_new and function_absent and trigger_absent),
        "schema_create_privilege_observed":permitted,
        "migration_count":2,
        "database_writes":0,
        "mexc_orders_sent":0,
        "live_authorization":False,
    }


async def apply(conn:Any,*,blobs:tuple[bytes,...])->dict[str,Any]:
    if type(blobs) is not tuple or len(blobs)!=2:
        raise V40JournalMigrationBlocked("V40_JOURNAL_MIGRATION_SET_INCOMPLETE")
    try:
        async with conn.transaction():
            await conn.execute("SET LOCAL lock_timeout='5s'")
            await conn.execute("SET LOCAL statement_timeout='30s'")
            acquired=await conn.fetchval(
                "SELECT pg_try_advisory_xact_lock(110040,110032)"
            )
            if acquired is not True:
                raise V40JournalMigrationBlocked("V40_JOURNAL_DDL_LOCK_BUSY")
            state=await plan(conn)
            if state["status"]!="V40_JOURNAL_CREATE_ONLY_PLAN_PASSED":
                raise V40JournalMigrationBlocked("V40_JOURNAL_DDL_PRECHECK_FAILED")
            for blob in blobs:
                await conn.execute(blob.decode("utf-8"))
            if not all(
                (await conn.fetchval("SELECT to_regclass($1)",name)) is not None
                for name in TARGET_OBJECTS
            ):
                raise V40JournalMigrationBlocked("V40_JOURNAL_SCHEMA_POSTCHECK_FAILED")
            exists=await conn.fetchval(
                """SELECT EXISTS (SELECT 1 FROM pg_trigger
                   WHERE tgname=$1 AND NOT tgisinternal)""",TRIGGER
            )
            if exists is not True:
                raise V40JournalMigrationBlocked("V40_JOURNAL_TRIGGER_NOT_PRESENT")
            for table in TARGET_OBJECTS[:2]:
                n=await conn.fetchval("SELECT count(*) FROM "+table)
                if type(n) is not int or n!=0:
                    raise V40JournalMigrationBlocked("V40_JOURNAL_NOT_EMPTY_AFTER_CREATE")
        return {
            "status":"V40_JOURNAL_031_032_CREATED_WITHOUT_RUNTIME_GRANTS",
            "database_writes":2,
            "tables_created":2,
            "immutability_trigger_present":True,
            "runtime_role_granted":False,
            "mexc_orders_sent":0,"live_authorization":False,
        }
    except V40JournalMigrationBlocked:
        raise
    except Exception:
        raise V40JournalMigrationBlocked("V40_JOURNAL_TRANSACTION_FAILED_RECONCILE") from None


async def run(*,apply_changes:bool,backup_export_verified:bool)->dict[str,Any]:
    if apply_changes and not backup_export_verified:
        raise V40JournalMigrationBlocked("V40_JOURNAL_VERIFIED_BACKUP_REQUIRED")
    blobs=pinned_migrations()
    if apply_changes:
        print("Adding ONLY V40 031+032 journal tables, indexes and immutable trigger.")
        if input(f"Type '{CONFIRM}': ").strip()!=CONFIRM:
            raise V40JournalMigrationBlocked("V40_JOURNAL_EXPLICIT_CONFIRMATION_REQUIRED")
    dsn=getpass.getpass("Existing PostgreSQL table-owner URL (hidden): ")
    if not dsn:
        raise V40JournalMigrationBlocked("V40_JOURNAL_OWNER_DSN_REQUIRED")
    import asyncpg
    try:
        conn=await asyncpg.connect(
            dsn=dsn,ssl="require",timeout=12,command_timeout=40,
        )
    except Exception:
        raise V40JournalMigrationBlocked("V40_JOURNAL_OWNER_CONNECT_FAILED") from None
    try:
        if not apply_changes:
            async with conn.transaction(readonly=True):
                return await plan(conn)
        return await apply(conn,blobs=blobs)
    finally:
        await conn.close()


def main()->None:
    p=argparse.ArgumentParser(description=__doc__)
    group=p.add_mutually_exclusive_group()
    group.add_argument("--plan",action="store_true")
    group.add_argument("--apply",action="store_true")
    p.add_argument("--backup-export-verified",action="store_true")
    args=p.parse_args()
    try:
        result=asyncio.run(run(
            apply_changes=args.apply,
            backup_export_verified=args.backup_export_verified,
        ))
        print(json.dumps(result,sort_keys=True))
        if result["status"] not in (
            "V40_JOURNAL_CREATE_ONLY_PLAN_PASSED",
            "V40_JOURNAL_031_032_CREATED_WITHOUT_RUNTIME_GRANTS",
        ):
            raise SystemExit(2)
    except V40JournalMigrationBlocked as exc:
        print(json.dumps({
            "status":str(exc),"database_writes_unverified":True,
            "mexc_orders_sent":0,"live_authorization":False,
        },sort_keys=True))
        raise SystemExit(2) from None


if __name__=="__main__":
    main()
