"""Independent guardian's immutable incident DB must permit fresh flat scope.

All tests are networkless. No MEXC API keys or trading permissions.
"""
import asyncio

import pytest

from app.t100_live_db_inspect import EXPECTED_DATABASE
from app.t100_live_role_preflight import TARGET_ROLE
from app.t100_v40_guardian_reviewed_generation import ACTIVE_REVIEWED_SCOPE
from app.t100_v40_guardian_post_gate import (
    PostgresV40GuardianPostGate,
)


class Context:
    def __init__(self,connection):
        self.connection=connection
    async def __aenter__(self):
        return self.connection
    async def __aexit__(self,*args):
        return False


class FakeConnection:
    def __init__(self):
        self.role=TARGET_ROLE
        self.db=EXPECTED_DATABASE
        self.readonly="on"
        self.isolation="repeatable read"
        self.state={
            "last_status":"OBSERVER_V40_CANARY_FLAT_DOUBLE_GET",
            "last_alert":False,
            "critical_latched":False,
            "revision":4,
            "fresh":True,
            "not_future":True,
        }
        self.outbox={"critical_total":0,"pending_total":0}
        self.queries=[]
    def transaction(self,**kwargs):
        assert kwargs=={"isolation":"repeatable_read","readonly":True}
        return Context(self)
    async def fetchval(self,sql):
        self.queries.append(sql)
        if sql=="SELECT current_user":return self.role
        if sql=="SELECT current_database()":return self.db
        if "transaction_read_only" in sql:return self.readonly
        if "transaction_isolation" in sql:return self.isolation
        raise AssertionError("unexpected query")
    async def fetchrow(self,sql,*args):
        assert args==(ACTIVE_REVIEWED_SCOPE,)
        self.queries.append(sql)
        if "t100_guardian_incident_state" in sql:
            assert "interval '15 seconds'" in sql
            return self.state
        if "t100_guardian_incident_outbox" in sql:
            assert "severity='critical'" in sql
            return self.outbox
        raise AssertionError("unexpected SQL")


class FakePool:
    def __init__(self,conn):
        self.conn=conn
    def acquire(self):
        return Context(self.conn)


def observe(conn):
    return asyncio.run(PostgresV40GuardianPostGate(FakePool(conn)).verify())


def test_verified_fresh_independent_guardian_is_not_itself_live_permission():
    conn=FakeConnection()
    out=observe(conn)
    assert out.state_verified and out.outbox_verified
    assert out.guardian_fresh and out.critical_latch_absent
    assert out.status=="V40_GUARDIAN_POST_GATE_CURRENT_FLAT_UNLATCHED_NONAUTHORIZING"
    assert out.entry_allowed is False and out.live_authorization is False
    assert out.mexc_posts==0 and out.database_writes==0
    assert len(conn.queries)==6


@pytest.mark.parametrize("field,value",[
    ("last_status","OBSERVER_V40_CANARY_SIGNED_GET_OR_ACCOUNT_INVALID"),
    ("last_alert",True),
    ("critical_latched",True),
    ("revision",1),
    ("fresh",False),
    ("not_future",False),
])
def test_critical_or_stale_guardian_never_allows_post(field,value):
    conn=FakeConnection()
    conn.state[field]=value
    out=observe(conn)
    assert out.status=="V40_GUARDIAN_POST_GATE_LATCH_OR_FRESHNESS_HALT"
    assert out.state_verified is False and out.live_authorization is False


@pytest.mark.parametrize("field,value",[
    ("critical_total",1),("pending_total",1),
])
def test_any_reviewed_scope_critical_or_unacked_alert_is_a_halt(field,value):
    conn=FakeConnection()
    conn.outbox[field]=value
    out=observe(conn)
    assert out.status=="V40_GUARDIAN_POST_GATE_OUTBOX_HALT"
    assert out.critical_latch_absent is False


@pytest.mark.parametrize("field,value",[
    ("role","mexc_scanner"),
    ("db","wrong_database"),
    ("readonly","off"),
    ("isolation","read committed"),
])
def test_untrusted_database_connection_never_allows_post(field,value):
    conn=FakeConnection()
    setattr(conn,field,value)
    out=observe(conn)
    assert out.status=="V40_GUARDIAN_POST_GATE_DB_ROLE_INVALID_NO_POST"


def test_query_error_or_secret_never_leaks():
    class BrokenPool:
        def acquire(self):
            raise RuntimeError("PRIVATE_DATABASE_CONNECTION_STRING")
    out=asyncio.run(PostgresV40GuardianPostGate(BrokenPool()).verify())
    assert out.status=="V40_GUARDIAN_POST_GATE_DATABASE_UNAVAILABLE_NO_POST"
    assert "PRIVATE_DATABASE_CONNECTION_STRING" not in repr(out)
    assert out.mexc_posts==0
