"""Operator-only V40 journal ACL commissioning: separate from signal reader.

The SOURCE role t100_v40_signal_reader is SELECT-only and MUST NOT acquire
journal grants. V40's existing dedicated execution runtime (if provisioned)
needs ONLY: SELECT/INSERT on reservation and immutable claim, plus
UPDATE(state, updated_at) on reservation. No DELETE/TRUNCATE/TRIGGER/DDL.
Do not create or rotate the execution role here.

Default --plan is READ ONLY. --apply requires an already independently
verified backup and a typed operator acknowledgment. Both operations use
a private owner DSN prompt; --apply also asks for the EXISTING runtime
role password and audits the effective privileges using a separate
fresh LOGIN AS t100_live_executor_runtime. No MEXC requests or order changes.

Critically: this module does not authorize live trading, change execution
flags or permit MEXC POSTs. Journal grants alone are not protection.
"""
from __future__ import annotations

import argparse
import asyncio
import getpass
import json
from typing import Any

from app.t100_live_db_inspect import EXPECTED_DATABASE
from app.t100_live_role_preflight import TARGET_ROLE
from app.t100_live_v40_stage2_source import RESTRICTED_V40_SIGNAL_READER_ROLE

SLOT="public.t100_live_v40_slot_reservations"
CLAIM="public.t100_live_v40_dispatch_claims"
TRIGGER="t100_v40_dispatch_claim_immutable"
UPDATE_COLUMNS=frozenset({"state","updated_at"})
CONFIRM="GRANT ONLY T100 V40 JOURNAL RUNTIME RIGHTS"


class V40JournalGrantsHalted(RuntimeError):
    """Fixed response statuses only: never expose DSN/credentials or raw SQL."""


def _verify_role(row:Any)->bool:
    return row is not None and all((
        row["rolcanlogin"] is True,
        row["rolinherit"] is False,
        row["rolsuper"] is False,
        row["rolcreatedb"] is False,
        row["rolcreaterole"] is False,
        row["rolreplication"] is False,
        row["rolbypassrls"] is False,
    ))


async def plan(conn: Any)->dict[str,Any]:
    """Only static SELECTs inside a caller-enforced READ ONLY transaction."""
    fail=lambda status: {
        "status":status,
        "runtime_role":TARGET_ROLE,
        "signal_reader_role":RESTRICTED_V40_SIGNAL_READER_ROLE,
        "database_writes":0,
        "mexc_posts":0,
        "live_authorization":False,
    }
    try:
        if (
            await conn.fetchval("SELECT current_setting('transaction_read_only')")!="on"
            or await conn.fetchval("SELECT current_database()")!=EXPECTED_DATABASE
        ):
            return fail("V40_JOURNAL_GRANTS_PLAN_CONTEXT_INVALID")
        existing=await conn.fetchrow(
            """SELECT rolcanlogin,rolinherit,rolsuper,rolcreatedb,
                      rolcreaterole,rolreplication,rolbypassrls
               FROM pg_catalog.pg_roles WHERE rolname=$1""",
            TARGET_ROLE,
        )
        if existing is None:
            return fail("V40_JOURNAL_RUNTIME_ROLE_ABSENT_USE_BASE_ROLE_SETUP")
        if not _verify_role(existing):
            return fail("V40_JOURNAL_RUNTIME_ROLE_ATTRIBUTES_UNSAFE")
        # Only the existing table OWNER may grant; using a privileged
        # arbitrary role or alternate role is intentionally not sufficient.
        owner=await conn.fetchval("SELECT current_user")
        for table in (SLOT,CLAIM):
            row=await conn.fetchrow(
                """SELECT pg_get_userbyid(relowner) AS owner
                   FROM pg_catalog.pg_class
                   WHERE oid=to_regclass($1) AND relkind='r'""",
                table,
            )
            if row is None:
                return fail("V40_JOURNAL_TABLES_MISSING")
            if row["owner"]!=owner:
                return fail("V40_JOURNAL_OWNER_MISMATCH")
            count=await conn.fetchval("SELECT count(*) FROM "+table)
            if type(count) is not int or count!=0:
                return fail("V40_JOURNAL_EXISTING_ROWS_MANUAL_REVIEW")
        trigger=await conn.fetchval(
            """SELECT EXISTS (
                SELECT 1 FROM pg_catalog.pg_trigger
                WHERE tgrelid=to_regclass($1) AND tgname=$2
                  AND tgenabled IN ('O','A') AND NOT tgisinternal
               )""",CLAIM,TRIGGER
        )
        if trigger is not True:
            return fail("V40_JOURNAL_IMMUTABLE_TRIGGER_NOT_ACTIVE")
        # Reject unexpected inherited/default grants before changing ACLs.
        for table in (SLOT,CLAIM):
            for privilege in (
                "SELECT","INSERT","UPDATE","DELETE","TRUNCATE",
                "REFERENCES","TRIGGER",
            ):
                granted=await conn.fetchval(
                    "SELECT has_table_privilege($1,$2,$3)",
                    TARGET_ROLE,table,privilege,
                )
                if granted is not False:
                    return fail("V40_JOURNAL_RUNTIME_EXISTING_ACL_REVIEW_REQUIRED")
            for column in (UPDATE_COLUMNS if table==SLOT else ("signal_id",)):
                granted=await conn.fetchval(
                    "SELECT has_column_privilege($1,$2,$3,'UPDATE')",
                    TARGET_ROLE,table,column,
                )
                if granted is not False:
                    return fail("V40_JOURNAL_RUNTIME_EXISTING_ACL_REVIEW_REQUIRED")
        for table in (SLOT,CLAIM):
            for privilege in ("SELECT","INSERT","UPDATE","DELETE","TRUNCATE"):
                granted=await conn.fetchval(
                    "SELECT has_table_privilege($1,$2,$3)",
                    RESTRICTED_V40_SIGNAL_READER_ROLE,table,privilege,
                )
                if granted is not False:
                    return fail("V40_JOURNAL_SIGNAL_READER_MUST_REMAIN_DENIED")
        return {
            **fail("V40_JOURNAL_RUNTIME_MINIMAL_GRANTS_PLAN_PASSED"),
            "owner_role_verified":True,
            "journal_tables_empty":True,
            "immutable_trigger_enabled":True,
            "runtime_role_attributes_verified":True,
            "signal_reader_journal_access_absent":True,
        }
    except Exception:
        return fail("V40_JOURNAL_GRANTS_PLAN_QUERY_FAILED")


GRANTS=(
    f"GRANT SELECT, INSERT ON TABLE {SLOT} TO {TARGET_ROLE}",
    f"GRANT UPDATE (state, updated_at) ON TABLE {SLOT} TO {TARGET_ROLE}",
    f"GRANT SELECT, INSERT ON TABLE {CLAIM} TO {TARGET_ROLE}",
)


async def audit_runtime(conn:Any)->dict[str,Any]:
    """Inspect actual authenticated runtime LOGIN, never owner impersonation."""
    def result(status:str)->dict[str,Any]:
        return {
            "status":status, "runtime_role":TARGET_ROLE,
            "journal_privileges_verified":False, "runtime_ddl_denied":False,
            "database_writes":0,"mexc_posts":0,"live_authorization":False,
        }
    try:
        async with conn.transaction(isolation="repeatable_read",readonly=True):
            if (
                await conn.fetchval("SELECT current_user")!=TARGET_ROLE
                or await conn.fetchval("SELECT current_database()")!=EXPECTED_DATABASE
                or await conn.fetchval("SELECT current_setting('transaction_read_only')")!="on"
            ):
                return result("V40_JOURNAL_RUNTIME_AUDIT_IDENTITY_HALT")
            for table in (SLOT,CLAIM):
                for privilege in ("SELECT","INSERT"):
                    if await conn.fetchval(
                        "SELECT has_table_privilege(current_user,$1,$2)",
                        table,privilege,
                    ) is not True:
                        return result("V40_JOURNAL_RUNTIME_EXPECTED_PRIVILEGES_MISSING")
                for privilege in ("DELETE","TRUNCATE","TRIGGER"):
                    if await conn.fetchval(
                        "SELECT has_table_privilege(current_user,$1,$2)",
                        table,privilege,
                    ) is not False:
                        return result("V40_JOURNAL_RUNTIME_FORBIDDEN_RIGHT_PRESENT")
                cols=await conn.fetch(
                    """SELECT column_name FROM information_schema.columns
                       WHERE table_schema='public' AND table_name=$1
                       ORDER BY ordinal_position""",
                    table.rsplit(".",1)[1],
                )
                if not cols:
                    return result("V40_JOURNAL_RUNTIME_COLUMNS_MISSING")
                for col in cols:
                    name=col["column_name"]
                    allowed=table==SLOT and name in UPDATE_COLUMNS
                    can=await conn.fetchval(
                        "SELECT has_column_privilege(current_user,$1,$2,'UPDATE')",
                        table,name,
                    )
                    if can is not allowed:
                        return result("V40_JOURNAL_RUNTIME_UPDATE_SCOPE_INVALID")
                # No whole-table UPDATE, only listed slot columns.
                if await conn.fetchval(
                    "SELECT has_table_privilege(current_user,$1,'UPDATE')",
                    table,
                ) is not False:
                    return result("V40_JOURNAL_RUNTIME_BROAD_UPDATE_INVALID")
            for privilege in ("CREATE",):
                if await conn.fetchval(
                    "SELECT has_schema_privilege(current_user,'public',$1)",
                    privilege,
                ) is not False:
                    return result("V40_JOURNAL_RUNTIME_SCHEMA_DDL_PRESENT")
                if await conn.fetchval(
                    "SELECT has_database_privilege(current_user,current_database(),$1)",
                    privilege,
                ) is not False:
                    return result("V40_JOURNAL_RUNTIME_DATABASE_DDL_PRESENT")
            active=await conn.fetchval(
                """SELECT EXISTS (
                   SELECT 1 FROM pg_catalog.pg_trigger
                   WHERE tgrelid=to_regclass($1) AND tgname=$2
                     AND tgenabled IN ('O','A') AND NOT tgisinternal
                )""",CLAIM,TRIGGER,
            )
            if active is not True:
                return result("V40_JOURNAL_RUNTIME_IMMUTABLE_TRIGGER_OFF")
        return {
            **result("V40_JOURNAL_RUNTIME_MINIMAL_PRIVILEGES_VERIFIED_NONAUTHORIZING"),
            "journal_privileges_verified":True,"runtime_ddl_denied":True,
            "immutable_trigger_verified":True,
        }
    except Exception:
        return result("V40_JOURNAL_RUNTIME_AUDIT_QUERY_FAILED")


async def apply(conn:Any)->dict[str,Any]:
    """Exactly three ACL GRANT statements in one owner transaction."""
    try:
        async with conn.transaction():
            await conn.execute("SET LOCAL lock_timeout='5s'")
            await conn.execute("SET LOCAL statement_timeout='30s'")
            if await conn.fetchval(
                "SELECT pg_try_advisory_xact_lock(110040,110033)"
            ) is not True:
                raise V40JournalGrantsHalted("V40_JOURNAL_GRANTS_LOCK_BUSY")
            # Validate current state in an explicit read-only transaction is
            # not possible within an already active write transaction.
            # The caller MUST have obtained the prior read-only plan; repeat
            # the same strict owner/table/trigger checks here.
            if await conn.fetchval("SELECT current_database()")!=EXPECTED_DATABASE:
                raise V40JournalGrantsHalted("V40_JOURNAL_GRANTS_WRONG_DB")
            if await conn.fetchval("SELECT current_user") is None:
                raise V40JournalGrantsHalted("V40_JOURNAL_GRANTS_OWNER_UNKNOWN")
            role=await conn.fetchrow(
                """SELECT rolcanlogin,rolinherit,rolsuper,rolcreatedb,
                          rolcreaterole,rolreplication,rolbypassrls
                   FROM pg_catalog.pg_roles WHERE rolname=$1""",TARGET_ROLE,
            )
            if not _verify_role(role):
                raise V40JournalGrantsHalted("V40_JOURNAL_GRANTS_ROLE_UNVERIFIED")
            for table in (SLOT,CLAIM):
                row=await conn.fetchrow(
                    """SELECT pg_get_userbyid(relowner) AS owner
                       FROM pg_catalog.pg_class
                       WHERE oid=to_regclass($1) AND relkind='r'""",table,
                )
                owner=await conn.fetchval("SELECT current_user")
                if row is None or row["owner"]!=owner:
                    raise V40JournalGrantsHalted("V40_JOURNAL_GRANTS_OWNER_INVALID")
                if await conn.fetchval("SELECT count(*) FROM "+table)!=0:
                    raise V40JournalGrantsHalted("V40_JOURNAL_GRANTS_NONEMPTY")
                for privilege in (
                    "SELECT","INSERT","UPDATE","DELETE","TRUNCATE",
                    "REFERENCES","TRIGGER",
                ):
                    if await conn.fetchval(
                        "SELECT has_table_privilege($1,$2,$3)",
                        TARGET_ROLE,table,privilege,
                    ) is not False:
                        raise V40JournalGrantsHalted("V40_JOURNAL_GRANTS_EXISTING_ACL")
                for column in (UPDATE_COLUMNS if table==SLOT else ("signal_id",)):
                    if await conn.fetchval(
                        "SELECT has_column_privilege($1,$2,$3,'UPDATE')",
                        TARGET_ROLE,table,column,
                    ) is not False:
                        raise V40JournalGrantsHalted("V40_JOURNAL_GRANTS_EXISTING_ACL")
                for privilege in ("SELECT","INSERT","UPDATE","DELETE","TRUNCATE"):
                    if await conn.fetchval(
                        "SELECT has_table_privilege($1,$2,$3)",
                        RESTRICTED_V40_SIGNAL_READER_ROLE,table,privilege,
                    ) is not False:
                        raise V40JournalGrantsHalted(
                            "V40_JOURNAL_GRANTS_SIGNAL_READER_NOT_ISOLATED"
                        )
            trigger=await conn.fetchval(
                """SELECT EXISTS (
                    SELECT 1 FROM pg_catalog.pg_trigger WHERE
                      tgrelid=to_regclass($1) AND tgname=$2
                      AND tgenabled IN ('O','A') AND NOT tgisinternal
                )""",CLAIM,TRIGGER,
            )
            if trigger is not True:
                raise V40JournalGrantsHalted("V40_JOURNAL_GRANTS_TRIGGER_INACTIVE")
            for sql in GRANTS:
                await conn.execute(sql)
        return {
            "status":"V40_JOURNAL_RUNTIME_GRANTS_APPLIED_AUDIT_PENDING",
            "grant_statements":3,"database_writes":3,"mexc_posts":0,
            "journal_privileges_verified":False,"live_authorization":False,
        }
    except V40JournalGrantsHalted:
        raise
    except Exception:
        raise V40JournalGrantsHalted("V40_JOURNAL_GRANTS_TRANSACTION_UNCERTAIN") from None


async def _run(*,apply_changes:bool)->dict[str,Any]:
    if apply_changes:
        if input(f"Type '{CONFIRM}': ").strip()!=CONFIRM:
            raise V40JournalGrantsHalted("V40_JOURNAL_GRANTS_CONFIRMATION_REQUIRED")
    owner_dsn=getpass.getpass("Existing database table-owner URL (hidden): ")
    if not owner_dsn:
        raise V40JournalGrantsHalted("V40_JOURNAL_GRANTS_OWNER_DSN_REQUIRED")
    import asyncpg
    try:
        conn=await asyncpg.connect(
            dsn=owner_dsn,ssl="require",timeout=12,command_timeout=35,
        )
    except Exception:
        raise V40JournalGrantsHalted("V40_JOURNAL_GRANTS_CONNECT_FAILED") from None
    try:
        if not apply_changes:
            async with conn.transaction(readonly=True):
                return await plan(conn)
        # A fresh --plan run is necessary, but a stale plan alone cannot
        # bypass the repeated write-time preconditions.
        async with conn.transaction(readonly=True):
            prepared=await plan(conn)
        if prepared["status"]!="V40_JOURNAL_RUNTIME_MINIMAL_GRANTS_PLAN_PASSED":
            return prepared
        # Verify the EXISTING runtime login BEFORE any GRANT. Without this,
        # a missing/wrong password could leave committed ACL changes that
        # cannot be independently audited by the operator.
        runtime_password=getpass.getpass(
            "Existing restricted executor-runtime DB password (hidden): "
        )
        if not runtime_password:
            return {
                **prepared,
                "status":"V40_JOURNAL_RUNTIME_PASSWORD_REQUIRED_BEFORE_GRANTS",
            }
        try:
            witness=await asyncpg.connect(
                dsn=owner_dsn,user=TARGET_ROLE,password=runtime_password,
                ssl="require",timeout=12,command_timeout=20,
            )
            try:
                async with witness.transaction(readonly=True):
                    identity_ok=(
                        await witness.fetchval("SELECT current_user")==TARGET_ROLE
                        and await witness.fetchval("SELECT current_database()")
                        ==EXPECTED_DATABASE
                        and await witness.fetchval(
                            "SELECT current_setting('transaction_read_only')"
                        )=="on"
                    )
            finally:
                await witness.close()
        except Exception:
            identity_ok=False
        if not identity_ok:
            return {
                **prepared,
                "status":"V40_JOURNAL_RUNTIME_LOGIN_FAILED_BEFORE_GRANTS",
            }
        grant_result=await apply(conn)
    finally:
        await conn.close()
    try:
        runtime=await asyncpg.connect(
            dsn=owner_dsn,user=TARGET_ROLE,password=runtime_password,
            ssl="require",timeout=12,command_timeout=20,
        )
        try:
            audit=await audit_runtime(runtime)
        finally:
            await runtime.close()
        return {
            **grant_result,**audit,
            "database_writes":grant_result["database_writes"],
            "grant_statements":grant_result["grant_statements"],
            "grants_applied":True,
        }
    except Exception:
        return {**grant_result,"status":"V40_JOURNAL_GRANTS_APPLIED_AUDIT_LOGIN_FAILED"}


def main()->None:
    p=argparse.ArgumentParser(description=__doc__)
    group=p.add_mutually_exclusive_group()
    group.add_argument("--plan",action="store_true")
    group.add_argument("--apply",action="store_true")
    args=p.parse_args()
    try:
        result=asyncio.run(_run(apply_changes=args.apply))
        print(json.dumps(result,sort_keys=True))
        if result["status"] not in (
            "V40_JOURNAL_RUNTIME_MINIMAL_GRANTS_PLAN_PASSED",
            "V40_JOURNAL_RUNTIME_MINIMAL_PRIVILEGES_VERIFIED_NONAUTHORIZING",
        ):
            raise SystemExit(2)
    except V40JournalGrantsHalted as err:
        print(json.dumps({
            "status":str(err),"database_writes_unverified":True,
            "mexc_posts":0,"live_authorization":False,
        },sort_keys=True))
        raise SystemExit(2) from None


if __name__=="__main__":
    main()
