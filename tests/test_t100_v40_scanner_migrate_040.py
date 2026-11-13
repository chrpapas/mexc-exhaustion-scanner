"""Scanner-owner 040 migration is explicit, atomic, idempotent, no MEXC."""
import asyncio
from pathlib import Path
from importlib.resources import files

import pytest

import app.t100_v40_scanner_migrate_040 as m


def env():
    return {
        m.FLAG:"true",
        "T100_V40_RENDER_SCANNER_ENABLED":"true",
        "T100_EXECUTION_MODE":"paper",
        "T100_LIVE_ORDER_API_ENABLED":"false",
        "T100_LIVE_KILL_SWITCH":"true",
        "EXECUTION_ENABLED":"false",
        "TRADING_MODE":"paper",
    }


class Tx:
    def __init__(self,conn):self.conn=conn
    async def __aenter__(self):
        self.conn.events.append("SERIALIZABLE_BEGIN")
        return self
    async def __aexit__(self,typ,val,trace):
        self.conn.events.append("ROLLBACK" if typ else "COMMIT")


class Conn:
    def __init__(self,*,exists=False,role="mexc_scanner",create=True,
                 fail=False,guardian_exists=True,runtime_exists=True):
        self.exists=exists;self.role=role;self.create=create
        self.guardian_exists=guardian_exists
        self.runtime_exists=runtime_exists
        self.fail=fail;self.events=[]
    async def __aenter__(self):return self
    async def __aexit__(self,*args):pass
    def transaction(self,**kwargs):
        assert kwargs=={"isolation":"serializable"}
        return Tx(self)
    async def fetchval(self,sql,*args):
        self.events.append("FETCH")
        if "to_regrole('t100_live_executor_runtime')" in sql:
            return self.runtime_exists
        if "to_regrole('t100_v40_guardian_reader')" in sql:
            return self.guardian_exists
        if "has_schema_privilege" in sql:return self.create
        if "current_user" in sql:return self.role
        if "current_database()" in sql:return "mexc_exhaustion"
        if "transaction_read_only" in sql:return "off"
        if "transaction_isolation" in sql:return "serializable"
        if "pg_advisory_xact_lock" in sql:
            self.events.append("040_LOCK")
            return None
        if "to_regclass" in sql:
            assert args==("public.t100_live_v40_emergency_close_claims",)
            return self.exists
        raise AssertionError(sql)
    async def execute(self,sql,*args):
        if sql.startswith("SET LOCAL"):
            self.events.append("DEADLINE")
            return
        assert "CREATE TABLE public.t100_live_v40_emergency_close_claims" in sql
        assert "CREATE TRIGGER t100_v40_emergency_claim_immutable" in sql
        assert ("TO t100_v40_guardian_reader;" in sql) == self.guardian_exists
        assert "INSERT INTO" not in sql
        assert "DROP TABLE" not in sql and "TRUNCATE TABLE" not in sql
        self.events.append("CREATE_040")
        if self.fail:
            raise RuntimeError("SENSITIVE_DB_OWNER_PASS")
        self.exists=True
    async def fetchrow(self,sql,*args):
        assert "pg_class c" in sql
        self.events.append("VERIFY_GRANTS")
        return dict(
            ordinary_table=True,exact_table=True,exact_columns=True,
            has_primary_key=True,immutable_trigger=True,
            exec_select=True,exec_insert=True,exec_mutation=False,
            guardian_select=self.guardian_exists,guardian_mutation=False,
        )


class Pool:
    def __init__(self,conn):self.conn=conn
    def acquire(self):return self.conn


def go(conn,config=None):
    return asyncio.run(
        m.migrate_scanner_040_once(Pool(conn),env() if config is None else config)
    )


def test_bundle_is_exact_reviewed_migration_excluding_outer_begin_commit():
    actual=m._reviewed_bundle()
    ref=Path("migrations/040_v40_emergency_close_once_claim.sql").read_text()
    assert " ".join(ref.replace("BEGIN;","").replace("COMMIT;","").split()) == " ".join(actual.split())
    assert "CREATE TABLE public.t100_live_v40_emergency_close_claims" in actual
    assert "CREATE FUNCTION public.t100_v40_deny_emergency_claim_change" in actual
    assert "DELETE FROM" not in actual
    assert "DROP TABLE" not in actual


def test_scanner_owner_can_create_one_immutable_close_claim_once():
    conn=Conn()
    r=go(conn)
    assert r.migration_committed and r.postflight_permissions_verified
    assert r.status=="V40_MIGRATION_040_CREATED_AND_GRANTS_VERIFIED"
    assert conn.events.index("040_LOCK")<conn.events.index("CREATE_040")
    assert conn.events.index("CREATE_040")<conn.events.index("VERIFY_GRANTS")
    assert conn.events[-1]=="COMMIT"
    assert conn.events.count("CREATE_040")==1

    second=go(conn)
    assert second.status=="V40_MIGRATION_040_ALREADY_PRESENT_VERIFIED"
    assert conn.events.count("CREATE_040")==1


@pytest.mark.parametrize("updates",[
    {m.FLAG:"false"},
    {"T100_EXECUTION_MODE":"live"},
    {"T100_LIVE_ORDER_API_ENABLED":"true"},
    {"T100_LIVE_KILL_SWITCH":"false"},
    {"T100_V40_RENDER_SCANNER_ENABLED":"false"},
    {"EXECUTION_ENABLED":"true"},
    {"TRADING_MODE":"live"},
])
def test_never_run_owner_ddl_when_not_exact_existing_paper_scanner(updates):
    conf=env();conf.update(updates)
    conn=Conn()
    r=go(conn,conf)
    assert not r.migration_committed
    assert conn.events==[]


def test_no_create_privilege_or_wrong_owner_do_not_mutate_database():
    for conn in (Conn(role="t100_live_executor_runtime"),Conn(create=False)):
        r=go(conn)
        assert not r.migration_committed
        assert "CREATE_040" not in conn.events


def test_failed_create_rolls_back_and_redacts_owner_secrets():
    conn=Conn(fail=True)
    result=go(conn)
    assert result.status=="V40_MIGRATION_040_HALTED_AT_EXECUTE_DDL"
    assert conn.events[-1]=="ROLLBACK"
    assert not result.migration_committed
    assert "SENSITIVE" not in repr(result)


def test_absent_optional_guardian_reader_role_is_reported_not_fabricated():
    conn=Conn(guardian_exists=False)
    result=go(conn)
    assert result.status=="V40_MIGRATION_040_CREATED_RUNTIME_ONLY_GUARDIAN_ROLE_ABSENT"
    assert result.migration_committed is True
    assert result.postflight_permissions_verified is True
    assert result.guardian_reader_grant_verified is False
    assert conn.events.count("CREATE_040")==1

    second=go(conn)
    assert second.status=="V40_MIGRATION_040_RUNTIME_ONLY_GUARDIAN_ROLE_ABSENT"
    assert conn.events.count("CREATE_040")==1


def test_missing_executor_runtime_db_role_blocks_creation():
    conn=Conn(runtime_exists=False)
    r=go(conn)
    assert r.status=="V40_MIGRATION_040_REQUIRED_EXECUTOR_ROLE_MISSING"
    assert not r.migration_committed
    assert "CREATE_040" not in conn.events
