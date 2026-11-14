"""Recovery 003 is audit-gated; never clears a previous guardian latch."""
import asyncio
from dataclasses import replace

import pytest

from app import t100_v40_guardian_recovery_003 as m
from app.t100_v40_guardian_reviewed_generation import (
    V40GuardianReviewedGenerationHalted,
)
from tests.test_t100_v40_canary_durable_guardian import env, flat, exposed


class DB:
    def __init__(self):
        self.states = {
            scope: dict(
                last_status=m.GOOD_STATUS, last_alert=False,
                critical_latched=True, revision=12,
                recently_healthy=True,
            )
            for scope in m._PREVIOUS
        }
        self.alerts = {
            scope: dict(critical_total=1,forbidden_critical=0,undelivered=0)
            for scope in m._PREVIOUS
        }
        self.states[m.RECOVERY_SCOPE_003] = None
        self.new=[]
        self.queries=[]

    async def fetchrow(self,sql,*args):
        self.queries.append((sql,args))
        if "INSERT INTO public.t100_guardian_incident_state" in sql:
            assert args==(m.RECOVERY_SCOPE_003,m.AUDIT_STATUS,False)
            self.new.append(args)
            return dict(revision=1,critical_latched=False)
        if "FROM public.t100_guardian_incident_state" in sql:
            return self.states[args[0]]
        if "FROM public.t100_guardian_incident_outbox" in sql:
            assert args[1]==m.HISTORIC_CRITICAL
            return self.alerts[args[0]]
        raise AssertionError("SQL must be select or one audited insertion")

    async def fetch(self,*args):
        return []

    async def fetchval(self,*args):
        return 0


async def call(db,answers=None):
    seen=[]
    results=list(answers if answers is not None else [
        replace(flat(),ledger_provenance_verified=True),
        replace(flat(),ledger_provenance_verified=True),
    ])
    async def observer(_):
        seen.append("independent_signed_GET_and_ledger")
        event=results.pop(0)
        if isinstance(event,Exception):
            raise event
        return event
    async def wait(t):
        assert t==3.0
        seen.append("wait")
    out=await m.select_third_reviewed_scope(
        db,env=env(),observe=observer,pause=wait,
    )
    return out,seen


def test_requires_all_three_old_sticky_latches_delivered_and_two_fresh_bookends():
    db=DB()
    result,seen=asyncio.run(call(db))
    assert result==(m.RECOVERY_SCOPE_003,
                   "NEW_RECOVERY_003_AFTER_THREE_HISTORIC_ALERTS_AND_DOUBLE_GET")
    assert seen==["independent_signed_GET_and_ledger","wait",
                  "independent_signed_GET_and_ledger"]
    assert len(db.new)==1
    assert all(db.states[s]["critical_latched"] for s in m._PREVIOUS)
    assert all("DELETE " not in sql and "TRUNCATE" not in sql
               and "SET critical_latched" not in sql for sql,_ in db.queries)


@pytest.mark.parametrize("scope",m._PREVIOUS)
@pytest.mark.parametrize("fault",[
    {"critical_total":0},{"critical_total":2},
    {"forbidden_critical":1},{"undelivered":1},
])
def test_any_unreviewed_alert_stops_recovery(scope,fault):
    db=DB()
    db.alerts[scope].update(fault)
    with pytest.raises(V40GuardianReviewedGenerationHalted,
                       match="ALERT_NOT_REVIEWED"):
        asyncio.run(call(db))
    assert not db.new


@pytest.mark.parametrize("fault",[
    {"last_status":m.HISTORIC_CRITICAL},{"last_alert":True},
    {"critical_latched":False},{"revision":1},
    {"recently_healthy":False},
])
def test_current_002_must_be_freshly_flat_and_preserved(fault):
    db=DB()
    db.states[m.RECOVERY_SCOPE_002].update(fault)
    with pytest.raises(V40GuardianReviewedGenerationHalted,
                       match="PREDECESSOR_STATE_NOT_RECOVERED"):
        asyncio.run(call(db))
    assert not db.new


@pytest.mark.parametrize("bad",[
    exposed(),RuntimeError("TOKEN_SECRET_EXAMPLE"),
])
def test_unsafe_account_or_read_error_denies_new_recovery(bad):
    db=DB()
    with pytest.raises(V40GuardianReviewedGenerationHalted,
                       match="FRESH_FLAT_UNVERIFIED") as err:
        asyncio.run(call(db,[flat(),bad]))
    assert "TOKEN_SECRET_EXAMPLE" not in str(err.value)
    assert not db.new


def test_existing_latched_003_cannot_be_reset_by_restart():
    db=DB()
    db.states[m.RECOVERY_SCOPE_003]=dict(
        last_status=m.HISTORIC_CRITICAL,
        last_alert=True,critical_latched=True,
    )
    result,seen=asyncio.run(call(db))
    assert result==(m.RECOVERY_SCOPE_003,"EXISTING_RECOVERY_003_SCOPE")
    assert seen==[] and not db.new


def test_scope_003_pinned_and_distinct_from_all_prior():
    assert m.ACTIVE_REVIEWED_SCOPE==m.RECOVERY_SCOPE_003
    assert len(set((*m._PREVIOUS,m.RECOVERY_SCOPE_003)))==4
