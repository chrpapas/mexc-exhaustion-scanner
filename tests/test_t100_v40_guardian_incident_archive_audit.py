"""SELECT-only guardian incident archive summary never repairs a latch."""
import asyncio
from contextlib import asynccontextmanager
from dataclasses import asdict

import pytest

from app.t100_v40_guardian_incident_archive_audit import (
    GuardianArchiveAuditHalted, audit_guardian_archive_readonly, _SCOPES,
)
from app.t100_v40_guardian_reviewed_generation import (
    GOOD_STATUS,HISTORIC_CRITICAL,
)


class Conn:
    def __init__(self):
        self.calls=[]
        self.readonly=True
        self.states={
            scope:dict(last_status=GOOD_STATUS,last_alert=False,
                critical_latched=True,revision=5,recent=False)
            for _,scope in _SCOPES
        }
        self.states[_SCOPES[-1][1]].update(revision=80,recent=True)
        self.counts={
            scope:dict(critical_total=3 if name=="recovery_003" else 1,
                benign_class_count=3 if name=="recovery_003" else 1,
                other_class_count=0,undelivered=0)
            for name,scope in _SCOPES
        }
    @asynccontextmanager
    async def transaction(self,*,isolation,readonly):
        self.calls.append(("tx",isolation,readonly))
        assert isolation=="repeatable_read" and readonly is True
        yield
    async def fetchval(self,sql):
        self.calls.append(("fetchval",sql))
        return "on" if self.readonly else "off"
    async def fetchrow(self,sql,*args):
        self.calls.append(("fetchrow",sql,args))
        if "t100_guardian_incident_state" in sql:
            return self.states.get(args[0])
        if "t100_guardian_incident_outbox" in sql:
            assert args[1]==HISTORIC_CRITICAL
            return self.counts[args[0]]
        raise AssertionError("unexpected SQL")


def test_archive_audit_is_exact_select_only_and_reports_003_latched():
    c=Conn()
    x=asyncio.run(audit_guardian_archive_readonly(c))
    assert x.status=="V40_GUARDIAN_ARCHIVE_READONLY_AUDIT_COMPLETE_NONAUTHORIZING"
    assert x.all_scopes_present
    assert x.every_critical_alert_delivered
    assert x.active_scope_003_critical_latched
    assert x.active_scope_003_currently_flat
    assert x.scopes[-1].critical_total==3
    assert x.scopes[-1].critical_other_class_count==0
    assert x.scopes[-1].undelivered_total==0
    assert x.incident_history_verified is False
    assert not x.entry_allowed and not x.live_authorization
    assert x.mexc_posts==0 and x.database_writes==0
    assert sum(k=="fetchrow" for k,*_ in c.calls)==8
    assert all("UPDATE" not in op[1].upper() and "DELETE" not in op[1].upper()
               for op in c.calls if op[0] in ("fetchrow","fetchval"))
    assert "scope_sha256" not in str(x.safe_json())


def test_unknown_critical_class_or_pending_alert_does_not_look_recovered():
    c=Conn()
    k=_SCOPES[-1][1]
    c.counts[k].update(critical_total=4,other_class_count=1,undelivered=2)
    x=asyncio.run(audit_guardian_archive_readonly(c))
    assert x.scopes[-1].critical_other_class_count==1
    assert x.scopes[-1].undelivered_total==2
    assert not x.every_critical_alert_delivered
    assert x.active_scope_003_critical_latched
    assert not x.live_authorization


@pytest.mark.parametrize("mutate,reason",[
    (lambda c:setattr(c,"readonly",False),"TRANSACTION_NOT_READONLY"),
    (lambda c:c.counts[_SCOPES[-1][1]].update(critical_total=4),"COUNTS_INCONSISTENT"),
    (lambda c:c.states[_SCOPES[-1][1]].update(revision=0),"STATE_INVALID"),
    (lambda c:c.states[_SCOPES[-1][1]].update(recent=None),"STATE_INVALID"),
])
def test_invalid_db_evidence_halts(mutate,reason):
    c=Conn()
    mutate(c)
    with pytest.raises(GuardianArchiveAuditHalted,match=reason):
        asyncio.run(audit_guardian_archive_readonly(c))


def test_scope_missing_reports_missing_without_creating():
    c=Conn()
    del c.states[_SCOPES[0][1]]
    x=asyncio.run(audit_guardian_archive_readonly(c))
    assert not x.all_scopes_present
    assert not x.scopes[0].state_exists
    assert x.scopes[-1].critical_latched is True
