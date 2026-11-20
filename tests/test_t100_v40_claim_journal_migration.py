"""Offline explicit V40 031+032 DB migration safeguards; no real DDL."""
from __future__ import annotations

import asyncio
import pytest

from app.t100_v40_claim_journal_migration import (
    BASE_TABLES, TARGET_OBJECTS, FUNCTION, TRIGGER,
    V40JournalMigrationBlocked, apply, plan, pinned_migrations,
    run, CONFIRM,
)


class Tx:
    async def __aenter__(self):return self
    async def __aexit__(self,*_):return False


class Connection:
    def __init__(self,*,db="mexc_exhaustion", base=True,
                 existing=None,creatable=True,lock=True,fail_sql=False,
                 trigger_exists=False):
        self.db=db
        self.base=base
        self.existing=set(existing or [])
        self.creatable=creatable
        self.lock=lock
        self.fail_sql=fail_sql
        self.trigger_exists=trigger_exists
        self.sql=[]
        self.sent=0
    def transaction(self,**kwargs):
        self.sql.append(("TRANSACTION",kwargs))
        return Tx()
    async def fetchrow(self,sql,*args):
        self.sql.append((sql,args))
        if "pg_catalog.pg_roles" in sql:
            return {"superuser":False,"username":"mexc_scanner"}
        raise AssertionError("unexpected read")
    async def fetchval(self,sql,*args):
        self.sql.append((sql,args))
        if "SELECT current_database" in sql:return self.db
        if "pg_try_advisory_xact_lock" in sql:return self.lock
        if "SELECT to_regclass" in sql:
            name=args[0]
            if name in BASE_TABLES:return name if self.base else None
            if name in TARGET_OBJECTS:
                if name in self.existing or self.sent>=2:return name
                return None
            raise AssertionError("unexpected regclass")
        if "SELECT to_regprocedure" in sql:
            return FUNCTION if self.trigger_exists or self.sent>=2 else None
        if "has_schema_privilege" in sql:return self.creatable
        if "SELECT NOT EXISTS" in sql:
            return not(self.trigger_exists or self.sent>=2)
        if "SELECT EXISTS (SELECT 1 FROM pg_trigger" in sql:
            return self.trigger_exists or self.sent>=2
        if "SELECT count(*) FROM public.t100_live_v40" in sql:
            return 0
        raise AssertionError("unrecognized SQL "+sql[:75])
    async def execute(self,sql,*args):
        self.sql.append((sql,args))
        if "CREATE TABLE public.t100_live_v40" in sql:
            self.sent+=1
        if self.fail_sql and "CREATE TABLE public.t100_live_v40" in sql:
            raise RuntimeError("SECRET_DSN_WITH_OWNER_PASSWORD")
        return "OK"


def test_git_blob_pins_match_reviewed_create_only_v40_sql():
    blobs=pinned_migrations()
    assert len(blobs)==2
    assert b"CREATE TABLE public.t100_live_v40_slot_reservations" in blobs[0]
    assert b"CREATE TABLE public.t100_live_v40_dispatch_claims" in blobs[1]
    assert b"CREATE TRIGGER t100_v40_dispatch_claim_immutable" in blobs[1]
    assert b"DELETE FROM" not in blobs[0]+blobs[1]


def test_readonly_plan_leaves_all_source_and_live_tables_untouched():
    c=Connection()
    async def get():
        async with c.transaction(readonly=True):
            return await plan(c)
    out=asyncio.run(get())
    assert out["status"]=="V40_JOURNAL_CREATE_ONLY_PLAN_PASSED"
    assert out["database_writes"]==0
    assert not out["live_authorization"]
    assert c.sent==0


@pytest.mark.parametrize("kw",[
    {"db":"other_db"}, {"base":False},
    {"existing":(TARGET_OBJECTS[0],)}, {"creatable":False},
    {"trigger_exists":True},
])
def test_invalid_preconditions_never_produce_apply_ready(kw):
    c=Connection(**kw)
    if kw.get("db"):
        with pytest.raises(V40JournalMigrationBlocked,match="WRONG_DATABASE"):
            asyncio.run(plan(c))
    else:
        report=asyncio.run(plan(c))
        assert report["status"]=="V40_JOURNAL_SCHEMA_OR_OWNER_NOT_READY"


def test_create_only_two_migrations_under_same_db_tx():
    c=Connection()
    result=asyncio.run(apply(c,blobs=pinned_migrations()))
    assert result["status"]=="V40_JOURNAL_031_032_CREATED_WITHOUT_RUNTIME_GRANTS"
    assert result["tables_created"]==2 and result["immutability_trigger_present"]
    assert c.sent==2
    assert result["runtime_role_granted"] is False
    assert result["mexc_orders_sent"]==0
    assert result["live_authorization"] is False
    assert len([x for x,_ in c.sql if x=="TRANSACTION"])==1


def test_missing_migrations_before_any_schema_mutation():
    c=Connection()
    with pytest.raises(V40JournalMigrationBlocked,match="SET_INCOMPLETE"):
        asyncio.run(apply(c,blobs=()))
    assert c.sql==[] and c.sent==0


def test_ddl_lock_conflict_refuses_to_mutate():
    c=Connection(lock=False)
    with pytest.raises(V40JournalMigrationBlocked,match="DDL_LOCK_BUSY"):
        asyncio.run(apply(c,blobs=pinned_migrations()))
    assert c.sent==0


def test_partially_present_previous_schema_blocks_before_write():
    c=Connection(existing=(TARGET_OBJECTS[1],))
    with pytest.raises(V40JournalMigrationBlocked,match="DDL_PRECHECK_FAILED"):
        asyncio.run(apply(c,blobs=pinned_migrations()))
    assert c.sent==0


def test_sql_error_never_includes_password_or_order_permission():
    c=Connection(fail_sql=True)
    with pytest.raises(V40JournalMigrationBlocked,match="TRANSACTION_FAILED_RECONCILE") as exc:
        asyncio.run(apply(c,blobs=pinned_migrations()))
    assert "SECRET_DSN" not in str(exc.value)


def test_apply_requires_verified_backup_before_prompt_or_db():
    with pytest.raises(V40JournalMigrationBlocked,match="VERIFIED_BACKUP_REQUIRED"):
        asyncio.run(run(apply_changes=True,backup_export_verified=False))
