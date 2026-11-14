"""One-shot migration 040 via the EXISTING scanner database session.

Render's separate external SQL interface cannot negotiate TLS to the
private scanner DB; the existing scanner can. This routine runs ONLY on
explicit opt-in on the known paper-only V40 source service, inside one SQL
transaction, with exact database identity, owner authority, a dedicated
advisory lock, fixed reviewed SQL bundle and post-commit verification.

Do NOT run automatic schema upgrades, touch any existing table, or leak a
database URL/error message. This does not authorize real MEXC POSTs.
"""
from __future__ import annotations

from dataclasses import dataclass
from importlib.resources import files
from typing import Any,Mapping

FLAG="T100_V40_MIGRATE_EMERGENCY_040_ONCE"
_OWNER="mexc_scanner"
_DB="mexc_exhaustion"
_ASSET="v40_emergency_040.sql"
_REL="public.t100_live_v40_emergency_close_claims"


@dataclass(frozen=True)
class V40Migration040Report:
    status:str
    account_flatness_unverified:bool=True
    migration_committed:bool=False
    postflight_permissions_verified:bool=False
    guardian_reader_grant_verified:bool=False
    prior_trading_journal_preserved:bool=True
    mexc_posts:int=0
    entry_allowed:bool=False
    live_authorization:bool=False


_SCHEMA_SQL="""
SELECT
  c.relkind='r' AS ordinary_table,
  c.relname='t100_live_v40_emergency_close_claims' AS exact_table,
  (SELECT count(*) FROM pg_attribute a
   WHERE a.attrelid=c.oid AND a.attnum>0 AND NOT a.attisdropped)=11
     AS exact_columns,
  (SELECT count(*) FROM pg_constraint t
   WHERE t.conrelid=c.oid AND t.contype='p')=1 AS has_primary_key,
  EXISTS(
    SELECT 1 FROM pg_trigger t
    WHERE t.tgrelid=c.oid
      AND t.tgname='t100_v40_emergency_claim_immutable'
      AND NOT t.tgisinternal AND t.tgenabled='O'
  ) AS immutable_trigger,
  has_table_privilege('t100_live_executor_runtime',
     'public.t100_live_v40_emergency_close_claims','SELECT') AS exec_select,
  has_table_privilege('t100_live_executor_runtime',
     'public.t100_live_v40_emergency_close_claims','INSERT') AS exec_insert,
  has_table_privilege('t100_live_executor_runtime',
     'public.t100_live_v40_emergency_close_claims',
     'UPDATE,DELETE,TRUNCATE,TRIGGER') AS exec_mutation,
  COALESCE(has_table_privilege(
     to_regrole('t100_v40_guardian_reader')::oid,
     'public.t100_live_v40_emergency_close_claims'::regclass,
     'SELECT'),FALSE) AS guardian_select,
  COALESCE(has_table_privilege(
     to_regrole('t100_v40_guardian_reader')::oid,
     'public.t100_live_v40_emergency_close_claims'::regclass,
     'INSERT,UPDATE,DELETE'),FALSE) AS guardian_mutation
FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace
WHERE n.nspname='public' AND c.relname='t100_live_v40_emergency_close_claims'
"""


def enabled(env:Mapping[str,str])->bool:
    return (
        env.get(FLAG)=="true"
        and env.get("T100_V40_RENDER_SCANNER_ENABLED")=="true"
        and env.get("T100_EXECUTION_MODE")=="paper"
        and env.get("T100_LIVE_ORDER_API_ENABLED")=="false"
        and env.get("T100_LIVE_KILL_SWITCH")=="true"
        and env.get("EXECUTION_ENABLED")=="false"
        and env.get("TRADING_MODE")=="paper"
    )


def _reviewed_bundle()->str:
    text=files("app").joinpath(_ASSET).read_text(encoding="utf-8")
    if (
        len(text)<1800 or len(text)>6000
        or "CREATE TABLE public.t100_live_v40_emergency_close_claims" not in text
        or "CREATE FUNCTION public.t100_v40_deny_emergency_claim_change" not in text
        or "CREATE TRIGGER t100_v40_emergency_claim_immutable" not in text
        or "GRANT SELECT, INSERT" not in text
        or "GRANT SELECT" not in text
        or "COMMIT;" in text or "\nBEGIN;" in text
        or "DROP TABLE" in text or "TRUNCATE TABLE" in text
        or "DELETE FROM" in text or "UPDATE public." in text
        or "ALTER " in text
    ):
        raise ValueError("NON_REVIEWED_V40_SQL_BUNDLE")
    return text


async def migrate_scanner_040_once(
    pool:Any,env:Mapping[str,str],
)->V40Migration040Report:
    block=lambda code:V40Migration040Report(code)
    if pool is None or not enabled(env):
        return block("V40_MIGRATION_040_OPT_IN_PAPER_SCANNER_REQUIRED")
    stage="ASSET"
    try:
        ddl=_reviewed_bundle()
        stage="CONNECT"
        async with pool.acquire() as conn:
            stage="BEGIN"
            async with conn.transaction(isolation="serializable"):
                stage="OWNER_CHECK"
                if (
                    await conn.fetchval("SELECT current_user")!=_OWNER
                    or await conn.fetchval("SELECT current_database()")!=_DB
                    or await conn.fetchval(
                        "SELECT current_setting('transaction_read_only')"
                    )!="off"
                    or await conn.fetchval(
                        "SELECT current_setting('transaction_isolation')"
                    )!="serializable"
                    or await conn.fetchval(
                        "SELECT has_schema_privilege(current_user,'public','CREATE')"
                    ) is not True
                ):
                    return block("V40_MIGRATION_040_TRUSTED_OWNER_NOT_VERIFIED")
                stage="DEADLINE"
                await conn.execute(
                    "SET LOCAL statement_timeout = '15000ms'"
                )
                stage="ADVISORY_LOCK"
                await conn.fetchval(
                    "SELECT pg_advisory_xact_lock(10040,40)"
                )
                stage="EXISTENCE_READ"
                exists=await conn.fetchval(
                    "SELECT to_regclass($1) IS NOT NULL",_REL,
                )
                if type(exists) is not bool:
                    raise ValueError("INVALID_EXISTENCE_RESULT")
                stage="REQUIRED_ROLE_CHECK"
                runtime_role=await conn.fetchval(
                    "SELECT to_regrole('t100_live_executor_runtime') IS NOT NULL"
                )
                guardian_role=await conn.fetchval(
                    "SELECT to_regrole('t100_v40_guardian_reader') IS NOT NULL"
                )
                if runtime_role is not True or type(guardian_role) is not bool:
                    return block("V40_MIGRATION_040_REQUIRED_EXECUTOR_ROLE_MISSING")
                if guardian_role is False:
                    # The separately running V40 guardian currently shares
                    # the restricted runtime SQL role (as configured).
                    # Do not silently CREATE an unreviewed database role.
                    optional_grant=(
                        "GRANT SELECT\n"
                        "  ON TABLE public.t100_live_v40_emergency_close_claims\n"
                        "  TO t100_v40_guardian_reader;\n"
                    )
                    if ddl.count(optional_grant)!=1:
                        raise ValueError("V40_GUARDIAN_OPTIONAL_GRANT_NOT_EXACT")
                    ddl=ddl.replace(optional_grant,"")
                created=False
                if exists is False:
                    # One reviewed CREATE-only bundle. If the runner fails
                    # mid-query, the surrounding transaction rolls back.
                    stage="EXECUTE_DDL"
                    await conn.execute(ddl)
                    created=True
                stage="VERIFY_SHAPE_AND_GRANTS"
                shape=await conn.fetchrow(_SCHEMA_SQL)
                if shape is None or any(
                    shape[key] is not expected for key,expected in (
                        ("ordinary_table",True),
                        ("exact_table",True),
                        ("exact_columns",True),
                        ("has_primary_key",True),
                        ("immutable_trigger",True),
                        ("exec_select",True),
                        ("exec_insert",True),
                        ("exec_mutation",False),
                        ("guardian_select",guardian_role),
                        ("guardian_mutation",False),
                    )
                ):
                    raise ValueError("V40_EMERGENCY_CLAIM_SCHEMA_OR_GRANTS_INVALID")
                # No existing trading / incident rows updated or deleted.
                report=V40Migration040Report(
                    ("V40_MIGRATION_040_CREATED_AND_GRANTS_VERIFIED"
                     if guardian_role else
                     "V40_MIGRATION_040_CREATED_RUNTIME_ONLY_GUARDIAN_ROLE_ABSENT")
                    if created else
                    ("V40_MIGRATION_040_ALREADY_PRESENT_VERIFIED"
                     if guardian_role else
                     "V40_MIGRATION_040_RUNTIME_ONLY_GUARDIAN_ROLE_ABSENT"),
                    migration_committed=True,
                    postflight_permissions_verified=True,
                    guardian_reader_grant_verified=guardian_role,
                )
        # Success is returned only AFTER asyncpg confirmed COMMIT.
        return report
    except Exception as exc:
        # SQLSTATE alone is a five-character public protocol category,
        # never the server SQL error, owner name, query, connection or DSN.
        state=getattr(exc,"sqlstate",None)
        if (
            type(state) is str and len(state)==5
            and state.isascii() and state.isalnum()
            and state==state.upper()
        ):
            return block("V40_MIGRATION_040_HALTED_AT_"+stage+
                         "_SQLSTATE_"+state)
        return block("V40_MIGRATION_040_HALTED_AT_"+stage)
