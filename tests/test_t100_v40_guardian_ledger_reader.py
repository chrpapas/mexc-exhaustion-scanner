"""Offline dedicated V40 guardian SQL grant and role checks, no mutations."""
import asyncio

import pytest

from app.t100_live_db_inspect import EXPECTED_DATABASE
from app.t100_v40_guardian_ledger_reader import (
    PostgresV40GuardianLedgerReader,V40GuardianLedgerHalted,
    V40_GUARDIAN_READER_ROLE,
)


class Pool:
    def __init__(self):
        self.calls=[]
        self.role=V40_GUARDIAN_READER_ROLE
        self.db=EXPECTED_DATABASE
        self.isolation="repeatable read"
        self.read_only="on"
        self.privileged=False
        self.denied=False
        self.fail=False
    def acquire(self):
        return self
    async def __aenter__(self):
        return self
    async def __aexit__(self,*args):
        return False
    def transaction(self,*,isolation,readonly):
        assert isolation=="repeatable_read" and readonly is True
        self.calls.append("BEGIN READ ONLY")
        return self
    async def fetchval(self,sql):
        self.calls.append(sql)
        if self.fail:
            raise RuntimeError("SENSITIVE_PASSWORD")
        return {
            "SELECT current_user":self.role,
            "SELECT current_database()":self.db,
            "SELECT current_setting('transaction_isolation')":self.isolation,
            "SELECT current_setting('transaction_read_only')":self.read_only,
        }[sql]
    async def fetch(self,sql,*args):
        self.calls.append(sql)
        if self.fail:
            raise RuntimeError("SENSITIVE_PASSWORD")
        if "has_table_privilege" in sql:
            assert len(args)==1 and len(args[0])==4
            return [
                {"can_select":not self.denied,
                 "can_mutate":self.privileged}
                for _ in args[0]
            ]
        assert sql.strip().startswith("SELECT")
        return []


def inspect(p):
    return asyncio.run(PostgresV40GuardianLedgerReader(p).read_snapshot())


def test_four_table_snapshot_requires_dedicated_select_only_role():
    p=Pool()
    s=inspect(p)
    assert s.readonly_repeatable_read
    assert s.checkpoints==[]
    assert s.revisions==[]
    assert s.intents==[]
    assert s.stop_amendments==[]
    assert p.calls[0]=="BEGIN READ ONLY"
    assert len([x for x in p.calls if "has_table_privilege" in x])==1
    assert len([x for x in p.calls if x.strip().startswith("SELECT ")
        and "FROM public." in x])==4
    assert not any(x.strip().startswith("INSERT ") for x in p.calls)


@pytest.mark.parametrize("change",[
    {"role":"t100_live_executor_runtime"},
    {"role":"postgres"},
    {"db":"wrong"},
    {"isolation":"read committed"},
    {"read_only":"off"},
    {"privileged":True},
    {"denied":True},
    {"fail":True},
])
def test_bad_role_dml_grants_or_db_outage_fail_closed_with_redacted_reason(change):
    p=Pool()
    for k,v in change.items():setattr(p,k,v)
    with pytest.raises(V40GuardianLedgerHalted) as e:
        inspect(p)
    assert "SENSITIVE_PASSWORD" not in str(e.value)
    assert not any(x.strip().startswith("INSERT ") for x in p.calls)


def test_no_pool_never_constructed():
    with pytest.raises(V40GuardianLedgerHalted):
        PostgresV40GuardianLedgerReader(None)
