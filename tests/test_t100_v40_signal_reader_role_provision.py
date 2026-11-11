"""No-network tests: narrowly scoped signal reader role and no trade access."""
from __future__ import annotations
import asyncio
import pytest

from app.t100_live_v40_stage2_source import RESTRICTED_V40_SIGNAL_READER_ROLE
from app.t100_v40_signal_reader_role_provision import (
    ROLE, SIGNAL_COLUMNS, CANDLE_COLUMNS, V40SourceRoleHalted,
    grant_sql, owner_plan, provision, independent_audit,
)


class Context:
    async def __aenter__(self):return self
    async def __aexit__(self,*_):return False


class FakeOwner:
    def __init__(self,*,user="mexc_exhaustion_owner",createrole=True,
                 exists=False,allowed=True,fail_ddl=False):
        self.user=user
        self.createrole=createrole
        self.exists=exists
        self.allowed=allowed
        self.fail_ddl=fail_ddl
        self.statements=[]
    def transaction(self,**kw):
        self.statements.append(("TRANSACTION",kw))
        return Context()
    async def fetchval(self,sql,*args):
        self.statements.append((sql,args))
        if "current_database" in sql:return "mexc_exhaustion"
        if "SELECT EXISTS" in sql:return self.exists
        if "has_table_privilege" in sql:return self.allowed
        raise AssertionError("unrecognized SQL")
    async def fetchrow(self,sql,*args):
        self.statements.append((sql,args))
        return {"name":self.user,"create_role":self.createrole,"super":False}
    async def execute(self,sql,*args):
        self.statements.append((sql,args))
        if self.fail_ddl and sql.startswith("CREATE ROLE"):
            raise ValueError("SECRET_DATABASE_CONNECTION")
        return "OK"


def test_role_is_never_shared_with_order_mutation_login():
    assert ROLE==RESTRICTED_V40_SIGNAL_READER_ROLE
    assert ROLE!="t100_live_executor_runtime"
    assert ROLE!="mexc_scanner"


def test_column_grants_are_exactly_two_source_tables_and_never_insert():
    grants=grant_sql()
    assert len(grants)==4
    assert "GRANT CONNECT" in grants[0]
    assert "GRANT USAGE" in grants[1]
    assert "SELECT (" in grants[2] and "t100_p2_signals" in grants[2]
    assert "SELECT (" in grants[3] and "candles" in grants[3]
    assert all(" INSERT " not in g and " UPDATE " not in g and
               " DELETE " not in g and " TRUNCATE " not in g
               for g in grants)
    assert "trade_decision" in SIGNAL_COLUMNS
    assert "amount" in CANDLE_COLUMNS


def test_plan_never_runs_ddl():
    c=FakeOwner()
    async def plan():
        async with c.transaction(readonly=True):
            return await owner_plan(c)
    report=asyncio.run(plan())
    assert report["status"]=="V40_SOURCE_ROLE_CREATE_PLAN_VALIDATED"
    assert report["database_writes"]==0
    assert not any("CREATE ROLE" in statement[0] for statement in c.statements)
    assert not report["live_authorization"]


@pytest.mark.parametrize("options,expected",[
    ({"createrole":False},"OWNER_CANNOT_CREATE_ROLE"),
    ({"exists":True},"ALREADY_EXISTS"),
    ({"allowed":False},"OWNER_CANNOT_READ_SOURCE"),
])
def test_owner_precheck_fails_closed(options,expected):
    with pytest.raises(V40SourceRoleHalted,match=expected):
        asyncio.run(owner_plan(FakeOwner(**options)))


def test_explicit_provision_has_no_order_or_source_table_writes():
    c=FakeOwner()
    out=asyncio.run(provision(c,password="long-password-1234567890-1234567890"))
    assert out["status"]=="V40_SOURCE_READER_CREATED_NEEDS_INDEPENDENT_ACL_AUDIT"
    calls=[x[0] for x in c.statements]
    assert any(x.startswith("CREATE ROLE t100_v40_signal_reader") for x in calls)
    assert not any(x.startswith(("INSERT","UPDATE","DELETE","TRUNCATE"))
                   for x in calls)
    assert len([x for x in calls if x.startswith("GRANT ")])==4
    assert not out["live_authorization"] and out["mexc_orders_sent"]==0


def test_bad_database_create_redacts_exception_and_no_partial_success():
    c=FakeOwner(fail_ddl=True)
    with pytest.raises(V40SourceRoleHalted,match="V40_SOURCE_ROLE_TRANSACTION_FAILED"):
        asyncio.run(provision(c,password="long-password-1234567890-1234567890"))


class FakeReader:
    def __init__(self, *, role=ROLE, unexpected_access=False):
        self.role=role
        self.unexpected_access=unexpected_access
    def transaction(self,**kw):
        assert kw=={"isolation":"repeatable_read","readonly":True}
        return Context()
    async def fetchval(self,sql,*args):
        if "has_column_privilege" in sql:return True
        if "has_table_privilege" in sql:return self.unexpected_access
        if sql=="SELECT current_user":return self.role
        if sql=="SELECT current_database()":return "mexc_exhaustion"
        raise AssertionError("unexpected SQL")
    async def fetch(self,sql,*args):
        assert "pg_catalog.pg_class" in sql
        return [{
            "rel":"public.candles","full_select":self.unexpected_access,
            "insert_ok":False,"update_ok":False,"delete_ok":False,
        }]


def test_independent_new_role_audit_positive_is_non_authorizing():
    out=asyncio.run(independent_audit(FakeReader()))
    assert out["status"]=="V40_SOURCE_READER_SELECT_ONLY_COLUMNS_AUDITED_NONAUTHORIZING"
    assert out["all_table_write_privileges_absent"]
    assert not out["live_authorization"]


def test_reader_audit_halts_on_wrong_role_or_broad_select():
    with pytest.raises(V40SourceRoleHalted,match="IDENTITY_INVALID"):
        asyncio.run(independent_audit(FakeReader(role="mexc_scanner")))
    with pytest.raises(V40SourceRoleHalted,match="UNEXPECTED_WRITE_PRIVILEGE"):
        asyncio.run(independent_audit(FakeReader(unexpected_access=True)))
