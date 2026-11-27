"""Provision a narrowly scoped READ-ONLY Stage2 source login (operator only).

This enables a future V40 executor to use TWO independent PostgreSQL
connections: source SELECT-only role and live-order journal role. It does
NOT grant source-table access to the order/ledger writer, mutate scanner
rows, authorize futures orders or deploy the unapplied V40 031/032 schema.

Default --plan is readonly. --apply requires a trusted DB owner, explicit
terminal confirmation and password typed twice without shell history.
No Render service or database changes are performed just by importing.
"""
from __future__ import annotations

import argparse
import asyncio
import getpass
import json
from typing import Any

from app.t100_live_db_inspect import EXPECTED_DATABASE
from app.t100_live_role_setup import _password_literal
from app.t100_live_v40_stage2_source import RESTRICTED_V40_SIGNAL_READER_ROLE

ROLE=RESTRICTED_V40_SIGNAL_READER_ROLE
CONFIRM=f"CREATE READONLY {ROLE} IN {EXPECTED_DATABASE}"
SIGNAL_COLUMNS=(
    "id","strategy_id","symbol","p2_at","feature_at","entry_price",
    "physical_episode_seq","stage_no","stage1_return_24h",
    "stage1_atr7_pct","stage1_strict365","stage1_eligible",
    "t100_tier","eligible_stage2","created_at","trade_decision",
)
CANDLE_COLUMNS=("symbol","interval","open_time","high","close","amount")


class V40SourceRoleHalted(RuntimeError):
    """Fixed statuses only: never include SQL, DSNs, passwords or URLs."""


def grant_sql()->tuple[str,...]:
    """SQL constants only: no caller-controlled role/table identifiers."""
    return (
        "GRANT CONNECT ON DATABASE mexc_exhaustion TO "+ROLE,
        "GRANT USAGE ON SCHEMA public TO "+ROLE,
        "GRANT SELECT ("+",".join(SIGNAL_COLUMNS)+
        ") ON public.t100_p2_signals TO "+ROLE,
        "GRANT SELECT ("+",".join(CANDLE_COLUMNS)+
        ") ON public.candles TO "+ROLE,
    )


async def owner_plan(conn:Any)->dict[str,Any]:
    """Role and owner facts inside a caller-enforced read-only transaction."""
    db=await conn.fetchval("SELECT current_database()")
    if db!=EXPECTED_DATABASE:
        raise V40SourceRoleHalted("V40_SOURCE_ROLE_WRONG_DATABASE")
    owner=await conn.fetchrow(
        "SELECT current_user AS name, "
        "r.rolcreaterole AS create_role, r.rolsuper AS super "
        "FROM pg_catalog.pg_roles r WHERE r.rolname=current_user"
    )
    if (
        owner is None
        or not (owner["create_role"] is True or owner["super"] is True)
    ):
        raise V40SourceRoleHalted("V40_SOURCE_ROLE_OWNER_CANNOT_CREATE_ROLE")
    exists=await conn.fetchval(
        "SELECT EXISTS (SELECT 1 FROM pg_catalog.pg_roles WHERE rolname=$1)",
        ROLE,
    )
    if exists is not False:
        raise V40SourceRoleHalted("V40_SOURCE_ROLE_ALREADY_EXISTS")
    for tab in ("public.t100_p2_signals","public.candles"):
        permitted=await conn.fetchval(
            "SELECT has_table_privilege(current_user,$1,'SELECT')",tab,
        )
        if permitted is not True:
            raise V40SourceRoleHalted("V40_SOURCE_ROLE_OWNER_CANNOT_READ_SOURCE")
    return {
        "status":"V40_SOURCE_ROLE_CREATE_PLAN_VALIDATED",
        "expected_role":ROLE,
        "source_table_count":2,
        "source_readonly_role_exists":False,
        "mexc_orders_sent":0,
        "database_writes":0,
        "live_authorization":False,
    }


async def provision(conn:Any, *, password:str)->dict[str,Any]:
    """One all-or-nothing transaction; no source-table data mutation."""
    literal=_password_literal(password)
    try:
        async with conn.transaction():
            await conn.execute("SET LOCAL lock_timeout = '5s'")
            await conn.execute("SET LOCAL statement_timeout = '30s'")
            plan=await owner_plan(conn)
            await conn.execute(
                "CREATE ROLE "+ROLE+
                " LOGIN NOINHERIT NOSUPERUSER NOCREATEDB NOCREATEROLE "
                "NOREPLICATION NOBYPASSRLS CONNECTION LIMIT 3 PASSWORD "+
                literal
            )
            for sql in grant_sql():
                await conn.execute(sql)
        return {**plan,
            "status":"V40_SOURCE_READER_CREATED_NEEDS_INDEPENDENT_ACL_AUDIT",
            "source_readonly_role_exists":True,
            "database_writes":1,
        }
    except V40SourceRoleHalted:
        raise
    except Exception:
        raise V40SourceRoleHalted("V40_SOURCE_ROLE_TRANSACTION_FAILED") from None


async def independent_audit(conn:Any)->dict[str,Any]:
    """Use ONLY a fresh connection authenticated as exact source reader."""
    async with conn.transaction(isolation="repeatable_read",readonly=True):
        role=await conn.fetchval("SELECT current_user")
        db=await conn.fetchval("SELECT current_database()")
        if role!=ROLE or db!=EXPECTED_DATABASE:
            raise V40SourceRoleHalted("V40_SOURCE_ROLE_INDEPENDENT_IDENTITY_INVALID")
        for tab,cols in (
            ("public.t100_p2_signals",SIGNAL_COLUMNS),
            ("public.candles",CANDLE_COLUMNS),
        ):
            for col in cols:
                ok=await conn.fetchval(
                    "SELECT has_column_privilege(current_user,$1,$2,'SELECT')",
                    tab,col,
                )
                if ok is not True:
                    raise V40SourceRoleHalted("V40_SOURCE_ROLE_SELECT_COLUMN_MISSING")
            for privilege in ("INSERT","UPDATE","DELETE","TRUNCATE","TRIGGER"):
                access=await conn.fetchval(
                    "SELECT has_table_privilege(current_user,$1,$2)",
                    tab,privilege,
                )
                if access is not False:
                    raise V40SourceRoleHalted("V40_SOURCE_ROLE_UNEXPECTED_WRITE_PRIVILEGE")
        source_tables=await conn.fetch(
            """SELECT c.oid::regclass::text AS rel,
               has_table_privilege(current_user,c.oid,'SELECT') AS full_select,
               has_table_privilege(current_user,c.oid,'INSERT') AS insert_ok,
               has_table_privilege(current_user,c.oid,'UPDATE') AS update_ok,
               has_table_privilege(current_user,c.oid,'DELETE') AS delete_ok
            FROM pg_catalog.pg_class c
            JOIN pg_catalog.pg_namespace n ON n.oid=c.relnamespace
            WHERE n.nspname='public' AND c.relkind IN ('r','p')"""
        )
        for row in source_tables:
            if any(row[k] is True for k in (
                "insert_ok","update_ok","delete_ok"
            )):
                raise V40SourceRoleHalted("V40_SOURCE_ROLE_WRITE_ACCESS_FOUND")
            # Full-table SELECT is disallowed even for the two source tables.
            if row["full_select"] is True:
                raise V40SourceRoleHalted("V40_SOURCE_ROLE_BROAD_SELECT_FOUND")
        return {
            "status":"V40_SOURCE_READER_SELECT_ONLY_COLUMNS_AUDITED_NONAUTHORIZING",
            "source_role":ROLE,
            "all_table_write_privileges_absent":True,
            "full_table_select_privileges_absent":True,
            "checked_tables":len(source_tables),
            "mexc_orders_sent":0,"live_authorization":False,
        }


async def run(*,apply:bool)->dict[str,Any]:
    import asyncpg
    if apply:
        if input("Type exact acknowledgment "+repr(CONFIRM)+": ").strip()!=CONFIRM:
            raise V40SourceRoleHalted("V40_SOURCE_ROLE_OPERATOR_CONFIRMATION_MISSING")
        secret=getpass.getpass("New source-reader DB password (32+ chars, hidden): ")
        secret_again=getpass.getpass("Repeat new DB password: ")
        if secret!=secret_again:
            raise V40SourceRoleHalted("V40_SOURCE_ROLE_PASSWORD_MISMATCH")
        _password_literal(secret)
    else:
        secret=None
    dsn=getpass.getpass("Existing PostgreSQL role-owner URL (hidden): ")
    if not dsn:
        raise V40SourceRoleHalted("V40_SOURCE_ROLE_OWNER_URL_MISSING")
    try:
        conn=await asyncpg.connect(dsn=dsn,ssl="require",timeout=10,
                                   command_timeout=35)
    except Exception:
        raise V40SourceRoleHalted("V40_SOURCE_ROLE_OWNER_CONNECTION_FAILED") from None
    try:
        if not apply:
            async with conn.transaction(readonly=True):
                return await owner_plan(conn)
        result=await provision(conn,password=secret)
    finally:
        await conn.close()
    try:
        verifier=await asyncpg.connect(
            dsn=dsn,user=ROLE,password=secret,ssl="require",timeout=10,
            command_timeout=20,
        )
        try:
            evidence=await independent_audit(verifier)
        finally:
            await verifier.close()
        return {**result,**evidence}
    except Exception:
        return {**result,
            "status":"V40_SOURCE_READER_CREATED_BUT_INDEPENDENT_AUDIT_FAILED",
            "live_authorization":False,
        }


def main()->None:
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply",action="store_true",
                        help="Create one restricted DB login with operator confirmation")
    args=parser.parse_args()
    try:
        result=asyncio.run(run(apply=args.apply))
        print(json.dumps(result,sort_keys=True))
    except V40SourceRoleHalted as error:
        print(json.dumps({
            "status":str(error),"mexc_orders_sent":0,
            "live_authorization":False,
        },sort_keys=True))
        raise SystemExit(2) from None


if __name__=="__main__":
    main()
