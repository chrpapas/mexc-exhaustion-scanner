"""V40 first-position journal must be empty under restricted SELECT-only login."""
import asyncio
import sys
from types import SimpleNamespace
from dataclasses import replace

import pytest
import app.t100_v40_render_journal_readonly as m

CLEAN={
    "status":"V40_DB_STATE_CLEAR_NONAUTHORIZING",
    "v40_slot_table_observed":True,
    "v40_dispatch_claim_table_observed":True,
    "v40_unresolved_dispatch_state_absent":True,
    "total_dispatch_claims":0,
    "protected_reservations":0,
}
LEGACY={field:0 for field in m.BASE_FIELDS}


class Tx:
    def __init__(self,kw):self.kw=kw
    async def __aenter__(self):return self
    async def __aexit__(self,*args):return False


class Conn:
    def __init__(self,*,user=m.TARGET_ROLE,slots=0,legacy=None,
                 db=m.EXPECTED_DATABASE,read_only="on",isolation="repeatable read"):
        self.user=user;self.slots=slots
        self.legacy=LEGACY if legacy is None else legacy
        self.db=db;self.read_only=read_only;self.isolation=isolation
        self.commands=[]
        self.closed=False
    def transaction(self,**kw):
        assert kw=={"isolation":"repeatable_read","readonly":True}
        return Tx(kw)
    async def fetchval(self,sql,*args):
        self.commands.append(sql)
        if sql=="SELECT current_database()":return self.db
        if sql=="SELECT current_user":return self.user
        if "transaction_read_only" in sql:return self.read_only
        if "transaction_isolation" in sql:return self.isolation
        if "count(*) FROM public.t100_live_v40_slot_reservations" in sql:
            return self.slots
        raise AssertionError("UNEXPECTED SQL")
    async def fetchrow(self,sql,*args):
        self.commands.append(sql)
        assert sql==m.BASE_UNRESOLVED_SQL
        return self.legacy
    async def close(self):self.closed=True
    async def execute(self,*_):
        raise AssertionError("NO DB WRITE MAY BE EXECUTED")


def run(conn,monkeypatch,state=None):
    async def assess(_conn):
        assert _conn is conn
        return dict(CLEAN) if state is None else state
    monkeypatch.setattr(m,"assess_v40_unresolved_dispatch_state",assess)
    async def target():
        async with conn.transaction(isolation="repeatable_read",readonly=True):
            return await m.inspect_runtime_journal(conn)
    return asyncio.run(target())


def test_restricted_empty_first_journal_is_readonly_not_authorizing(monkeypatch):
    conn=Conn()
    out=run(conn,monkeypatch)
    assert out.status=="V40_FIRST_POSITION_JOURNAL_EMPTY_AND_READONLY_VERIFIED"
    assert out.runtime_role_verified and out.first_position_journal_clear
    assert out.v40_no_prior_claims_or_slots and out.legacy_unresolved_absent
    assert out.database_writes==0 and out.mexc_posts==0
    assert out.entry_allowed is False and out.live_authorization is False
    assert not any(x.startswith(("INSERT","UPDATE","DELETE","TRUNCATE","GRANT")) for x in conn.commands)


@pytest.mark.parametrize("kw",[
    {"user":"t100_v40_signal_reader"},
    {"user":"mexc_scanner"},
    {"db":"other_db"},
    {"read_only":"off"},
    {"isolation":"read committed"},
])
def test_fails_wrong_identity_or_transaction_before_state_query(monkeypatch,kw):
    async def forbidden(_conn):
        raise AssertionError("should not fetch journal")
    monkeypatch.setattr(m,"assess_v40_unresolved_dispatch_state",forbidden)
    conn=Conn(**kw)
    out=asyncio.run(m.inspect_runtime_journal(conn))
    assert out.status=="V40_RUNTIME_JOURNAL_ROLE_OR_READONLY_CONTEXT_HALT"
    assert not out.first_position_journal_clear


@pytest.mark.parametrize("state,expected",[
    ({**CLEAN,"status":"V40_DB_DISPATCH_OR_RESERVATION_UNRESOLVED"},"V40_RUNTIME_JOURNAL_UNRESOLVED_OR_SCHEMA_HALT"),
    ({**CLEAN,"v40_slot_table_observed":False},"V40_RUNTIME_JOURNAL_UNRESOLVED_OR_SCHEMA_HALT"),
    ({**CLEAN,"total_dispatch_claims":1},"V40_RUNTIME_JOURNAL_PRIOR_CLAIM_REVIEW_REQUIRED"),
    ({**CLEAN,"protected_reservations":1},"V40_RUNTIME_JOURNAL_PRIOR_CLAIM_REVIEW_REQUIRED"),
])
def test_claim_or_reservation_ambiguity_is_not_automatically_cleared(monkeypatch,state,expected):
    out=run(Conn(),monkeypatch,state=state)
    assert out.status==expected
    assert not out.first_position_journal_clear


def test_released_slot_is_still_blocking_first_pilot(monkeypatch):
    out=run(Conn(slots=1),monkeypatch)
    assert out.status=="V40_RUNTIME_JOURNAL_PRIOR_SLOT_REVIEW_REQUIRED"


@pytest.mark.parametrize("field",m.BASE_FIELDS)
def test_legacy_pending_state_prevents_first_pilot(monkeypatch,field):
    out=run(Conn(legacy={**LEGACY,field:1}),monkeypatch)
    assert out.status=="V40_RUNTIME_JOURNAL_BASE_MUTATIONS_UNRESOLVED"


def test_no_dsn_makes_zero_database_calls():
    out=asyncio.run(m.observe_render_runtime_journal({}))
    assert out.status=="V40_RUNTIME_JOURNAL_RESTRICTED_EXECUTOR_DSN_MISSING"


def test_authenticated_runtime_role_and_readonly_tx_real_adapter_mock(monkeypatch):
    c=Conn()
    async def fake_connect(**params):
        assert params["ssl"]=="require"
        assert "SECRET" in params["dsn"]
        return c
    async def fake_state(conn):
        return dict(CLEAN)
    monkeypatch.setitem(sys.modules,"asyncpg",SimpleNamespace(connect=fake_connect))
    monkeypatch.setattr(m,"assess_v40_unresolved_dispatch_state",fake_state)
    out=asyncio.run(m.observe_render_runtime_journal({
        m.DSN_KEY:"postgresql://t100_live_executor_runtime:SECRET@host/mexc_exhaustion",
    }))
    assert out.first_position_journal_clear
    assert c.closed is True
    assert "SECRET" not in str(out)
