"""V40 minimum privilege gate tests: no direct MEXC, no DB writes from plan."""
import asyncio

import pytest

from app.t100_v40_journal_runtime_grants import (
    SLOT,CLAIM,TARGET_ROLE,RESTRICTED_V40_SIGNAL_READER_ROLE,
    GRANTS,V40JournalGrantsHalted,plan,audit_runtime,apply,
)


def role():
    return {
        "rolcanlogin":True,"rolinherit":False,"rolsuper":False,
        "rolcreatedb":False,"rolcreaterole":False,
        "rolreplication":False,"rolbypassrls":False,
    }


class Tx:
    async def __aenter__(self):return self
    async def __aexit__(self,*args):return False


class FakeOwner:
    def __init__(self,*,exists=True,trigger=True,owner="mexc_scanner",
                 count=0,existing_acl=False,role_attributes=None,lock=True):
        self.exists=exists
        self.trigger=trigger
        self.owner=owner
        self.count=count
        self.existing_acl=existing_acl
        self.role_attributes=role_attributes
        self.lock=lock
        self.calls=[]
        self.writes=[]
    def transaction(self,**kw):
        self.calls.append(("transaction",kw))
        return Tx()
    async def fetchval(self,sql,*args):
        self.calls.append((sql,args))
        if "transaction_read_only" in sql:return "on"
        if "current_database" in sql:return "mexc_exhaustion"
        if sql=="SELECT current_user":return self.owner
        if sql=="SELECT count(*) FROM "+SLOT or sql=="SELECT count(*) FROM "+CLAIM:
            return self.count
        if "pg_try_advisory_xact_lock" in sql:return self.lock
        if "pg_catalog.pg_trigger" in sql:return self.trigger
        if "has_table_privilege" in sql:return self.existing_acl
        if "has_column_privilege" in sql:return self.existing_acl
        raise AssertionError("unexpected sql "+sql[:100])
    async def fetchrow(self,sql,*args):
        self.calls.append((sql,args))
        if "pg_catalog.pg_roles" in sql:
            if not self.exists:return None
            return role() if self.role_attributes is None else self.role_attributes
        if "pg_catalog.pg_class" in sql:
            return {"owner":self.owner}
        raise AssertionError("unexpected SQL")
    async def execute(self,sql,*args):
        self.writes.append(sql)
        return "OK"


def runplan(c):
    async def run():
        async with c.transaction(readonly=True):
            return await plan(c)
    return asyncio.run(run())


def test_writes_and_targets_are_exactly_three_acl_statements():
    assert len(GRANTS)==3
    assert all(g.startswith("GRANT ") for g in GRANTS)
    assert all(" TO "+TARGET_ROLE in g for g in GRANTS)
    assert "UPDATE (state, updated_at)" in GRANTS[1]
    assert SLOT in GRANTS[0] and SLOT in GRANTS[1]
    assert CLAIM in GRANTS[2]
    assert all(RESTRICTED_V40_SIGNAL_READER_ROLE not in g for g in GRANTS)
    assert all("DELETE " not in g and "TRUNCATE " not in g for g in GRANTS)


def test_plan_is_read_only_and_non_authorizing():
    c=FakeOwner()
    out=runplan(c)
    assert out["status"]=="V40_JOURNAL_RUNTIME_MINIMAL_GRANTS_PLAN_PASSED"
    assert out["immutable_trigger_enabled"]
    assert out["database_writes"]==0
    assert not out["live_authorization"] and not out["mexc_posts"]
    assert c.writes==[]


def test_runtime_role_absent_cannot_receive_grants():
    c=FakeOwner(exists=False)
    out=runplan(c)
    assert out["status"]=="V40_JOURNAL_RUNTIME_ROLE_ABSENT_USE_BASE_ROLE_SETUP"
    assert c.writes==[]


@pytest.mark.parametrize("kw,status",[
    ({"trigger":False},"V40_JOURNAL_IMMUTABLE_TRIGGER_NOT_ACTIVE"),
    ({"count":1},"V40_JOURNAL_EXISTING_ROWS_MANUAL_REVIEW"),
    ({"owner":"not_owner"},"V40_JOURNAL_RUNTIME_MINIMAL_GRANTS_PLAN_PASSED"),
    ({"existing_acl":True},"V40_JOURNAL_RUNTIME_EXISTING_ACL_REVIEW_REQUIRED"),
    ({"role_attributes":{**role(),"rolsuper":True}},"V40_JOURNAL_RUNTIME_ROLE_ATTRIBUTES_UNSAFE"),
])
def test_preflight_review_states(kw,status):
    assert runplan(FakeOwner(**kw))["status"]==status


class RuntimeConn:
    def __init__(self,*,user=TARGET_ROLE,bad_column=None,wide_update=False,
                 missing_permission=False,ddl=False):
        self.user=user
        self.bad_column=bad_column
        self.wide_update=wide_update
        self.missing_permission=missing_permission
        self.ddl=ddl
    def transaction(self,**kw):
        assert kw=={"isolation":"repeatable_read","readonly":True}
        return Tx()
    async def fetchval(self,sql,*args):
        if "has_table_privilege" in sql:
            priv=args[1] if len(args)==2 else "UPDATE"
            if priv=="UPDATE":return self.wide_update
            if priv in ("SELECT","INSERT"):return not self.missing_permission
            return False
        if "has_column_privilege" in sql:
            table,col=args
            expected=(table==SLOT and col in ("state","updated_at"))
            if self.bad_column==col:return not expected
            return expected
        if "has_schema_privilege" in sql or "has_database_privilege" in sql:
            return self.ddl
        if "pg_catalog.pg_trigger" in sql:return True
        if sql=="SELECT current_user":return self.user
        if sql=="SELECT current_database()":return "mexc_exhaustion"
        if "transaction_read_only" in sql:return "on"
        raise AssertionError("unexpected SQL")
    async def fetch(self,sql,*args):
        if "information_schema.columns" in sql:
            if args[0]==SLOT.split(".")[1]:
                cols=("signal_id","symbol","external_oid","state","updated_at")
            else:
                cols=("signal_id","external_oid","state","claimed_at")
            return [{"column_name":c} for c in cols]
        raise AssertionError("unexpected SQL")


def check_audit(r):
    return asyncio.run(audit_runtime(r))


def test_independent_restricted_role_positive_does_not_authorize_trading():
    out=check_audit(RuntimeConn())
    assert out["status"]=="V40_JOURNAL_RUNTIME_MINIMAL_PRIVILEGES_VERIFIED_NONAUTHORIZING"
    assert out["journal_privileges_verified"]
    assert out["runtime_ddl_denied"]
    assert not out["live_authorization"]
    assert not out["mexc_posts"]


@pytest.mark.parametrize("options,status",[
    ({"user":"t100_v40_signal_reader"},"AUDIT_IDENTITY_HALT"),
    ({"bad_column":"signal_id"},"UPDATE_SCOPE_INVALID"),
    ({"missing_permission":True},"EXPECTED_PRIVILEGES_MISSING"),
    ({"wide_update":True},"BROAD_UPDATE_INVALID"),
    ({"ddl":True},"SCHEMA_DDL_PRESENT"),
])
def test_bad_runtime_privileges_refuse_authorization(options,status):
    out=check_audit(RuntimeConn(**options))
    assert status in out["status"]
    assert not out["journal_privileges_verified"]


def test_apply_uses_only_static_target_grants_no_other_services():
    c=FakeOwner()
    out=asyncio.run(apply(c))
    assert out["status"]=="V40_JOURNAL_RUNTIME_GRANTS_APPLIED_AUDIT_PENDING"
    assert c.writes[:2]==[
        "SET LOCAL lock_timeout='5s'",
        "SET LOCAL statement_timeout='30s'",
    ]
    assert c.writes[2:]==list(GRANTS)
    assert not out["live_authorization"]


def test_apply_rejects_existing_rights_before_grant():
    c=FakeOwner(existing_acl=True)
    with pytest.raises(V40JournalGrantsHalted,match="EXISTING_ACL"):
        asyncio.run(apply(c))
    assert c.writes[2:]==[]
