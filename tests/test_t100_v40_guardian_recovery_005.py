"""Audited one-time incident-preserving V40 guardian generation 005.

All SQL / signed exchange GETs synthetic; never trades or clears 004.
Code cannot emit 005 without exact prior incident history and TWO fresh
same-account FLAT / hedge / ledger / stop-order observations.
"""
from __future__ import annotations

import asyncio
from dataclasses import replace
import pytest

from app import t100_v40_guardian_recovery_005 as m
from tests.test_t100_v40_canary_durable_guardian import flat,exposed


class DB:
    def __init__(self):
        self.states={
            scope:dict(last_status=m.GOOD_STATUS,last_alert=False,
                       critical_latched=True,revision=180,
                       recently_healthy=True)
            for scope,_ in m._PREDECESSORS
        }
        self.states[m.RECOVERY_SCOPE_005]=None
        self.outboxes={
            scope:dict(
                total=n,observer_critical=n,other_critical=0,pending=0,
                nov15_incident_in_window=(1 if scope==m.RECOVERY_SCOPE_004 else 0),
                nov16_incident_in_window=(1 if scope==m.RECOVERY_SCOPE_004 else 0),
                nov17_incident_in_window=(1 if scope==m.RECOVERY_SCOPE_004 else 0),
            )
            for scope,n in m._PREDECESSORS
        }
        self.older={k:v.copy() for k,v in self.states.items() if v is not None}
        self.calls=[]
        self.inserts=[]
        self.scope_increment=0
    async def fetchrow(self,sql,*args):
        self.calls.append((sql,args))
        if "INSERT INTO public.t100_guardian_incident_state" in sql:
            assert args==(m.RECOVERY_SCOPE_005,m.AUDIT_STATUS,False)
            self.inserts.append(args)
            return dict(revision=1,critical_latched=False)
        if "FROM public.t100_guardian_incident_state" in sql:
            return self.states.get(args[0])
        if "FROM public.t100_guardian_incident_outbox" in sql:
            assert args[1]==m.HISTORIC_CRITICAL
            return self.outboxes[args[0]]
        raise AssertionError("Unexpected SQL for audited scope")
    async def fetch(self,*args):return []
    async def fetchval(self,*args):return 0


def env():
    return {
        "T100_V40_GUARDIAN_AUDITED_ROLLOVER":"true",
        "T100_V40_GUARDIAN_RECOVERY_005_OPERATOR_APPROVED":"true",
    }


async def call(db,readings=None,options=None):
    choices=iter(readings if readings is not None else [
        replace(flat(),ledger_provenance_verified=True),
        replace(flat(),ledger_provenance_verified=True),
    ])
    observed=[]
    waits=[]
    async def observe(_env):
        observed.append("INDEPENDENT_SIGNED_ACCOUNT_AND_LEDGER")
        x=next(choices)
        if isinstance(x,Exception):raise x
        return x
    async def pause(seconds):
        waits.append(seconds)
    result=await m.select_fifth_reviewed_scope(
        db,env=options if options is not None else env(),
        observe=observe,pause=pause,
    )
    return result,observed,waits


def test_exact_five_predecessor_scopes_three_004_events_and_double_get():
    db=DB()
    result,seen,waits=asyncio.run(call(db))
    assert result[0]==m.RECOVERY_SCOPE_005
    assert result[1]=="NEW_RECOVERY_005_AFTER_FIVE_REVIEWED_SCOPES_AND_DOUBLE_SIGNED_GET"
    assert seen==["INDEPENDENT_SIGNED_ACCOUNT_AND_LEDGER"]*2
    assert waits==[3.0]
    assert len(db.inserts)==1
    assert all(db.states[scope]==db.older[scope]
               for scope,_ in m._PREDECESSORS)
    assert all(args[0]==m.RECOVERY_SCOPE_005
               for sql,args in db.calls if sql.lstrip().startswith("INSERT"))


@pytest.mark.parametrize("options",[
    {},{"T100_V40_GUARDIAN_AUDITED_ROLLOVER":"true"},
    {"T100_V40_GUARDIAN_RECOVERY_005_OPERATOR_APPROVED":"true"},
])
def test_manual_optin_required_even_for_healthy_flat_account(options):
    db=DB()
    with pytest.raises(m.V40GuardianRecovery005Halted,match="OPERATOR_REVIEW_REQUIRED"):
        asyncio.run(call(db,options=options))
    assert db.inserts==[]


@pytest.mark.parametrize("idx,field,value",[
    (0,"total",2),(1,"observer_critical",0),
    (2,"pending",1),(3,"other_critical",1),
    (3,"total",4),(4,"total",2),(4,"total",4),
    (4,"observer_critical",0),(4,"other_critical",1),
    (4,"pending",1),(4,"nov15_incident_in_window",0),
    (4,"nov15_incident_in_window",2),
    (4,"nov16_incident_in_window",0),
    (4,"nov16_incident_in_window",2),
    (4,"nov17_incident_in_window",0),
    (4,"nov17_incident_in_window",2),
])
def test_unexpected_critical_or_unacknowledged_alert_blocks_new_scope(idx,field,value):
    db=DB()
    scope=m._PREDECESSORS[idx][0]
    db.outboxes[scope][field]=value
    with pytest.raises(m.V40GuardianRecovery005Halted,
                       match="INCIDENT_HISTORY_OR_DELIVERY_MISMATCH"):
        asyncio.run(call(db))
    assert db.inserts==[]


@pytest.mark.parametrize("idx,field,value",[
    (0,"last_status","OBSERVER_UNVERIFIED"),
    (1,"critical_latched",False),
    (2,"last_alert",True),(3,"revision",0),
    (4,"last_alert",True),(4,"critical_latched",False),
    (4,"recently_healthy",False),(4,"revision",2),
])
def test_predecessors_must_remain_latched_but_recently_healthy(idx,field,value):
    db=DB()
    scope=m._PREDECESSORS[idx][0]
    db.states[scope][field]=value
    with pytest.raises(m.V40GuardianRecovery005Halted,
                       match="PREDECESSOR_NOT_HEALTHY_OR_STICKY"):
        asyncio.run(call(db))
    assert db.inserts==[]


@pytest.mark.parametrize("bad",[
    exposed(),flat(),
    replace(flat(),ledger_provenance_verified=False),
    replace(flat(),inventory_two_signed_get_bookends=False),
    replace(flat(),hedge_mode_verified=False),
    replace(flat(),monitored_account_flat=False),
    replace(flat(),incident_alert=True),
    RuntimeError("PRIVATE_URL_OR_API_TOKEN"),
])
def test_missing_stops_or_ledger_or_unexpected_position_prevents_rollover(bad):
    db=DB()
    with pytest.raises(m.V40GuardianRecovery005Halted,
                       match="FRESH_(FLAT_HEDGE_LEDGER_UNVERIFIED|SIGNED_GET_UNAVAILABLE)"):
        asyncio.run(call(db,[
            replace(flat(),ledger_provenance_verified=True),bad,
        ]))
    assert not db.inserts


def test_existing_fifth_scope_is_sticky_even_if_flat():
    db=DB()
    db.states[m.RECOVERY_SCOPE_005]=dict(
        last_status=m.GOOD_STATUS,
        last_alert=False,critical_latched=True,
    )
    result,seen,wait=asyncio.run(call(db,[]))
    assert result==(m.RECOVERY_SCOPE_005,"EXISTING_RECOVERY_005_SCOPE_STICKY")
    assert not seen and not wait and not db.inserts


def test_existing_fifth_scope_invalid_never_overwritten():
    db=DB()
    db.states[m.RECOVERY_SCOPE_005]=dict(
        last_status="UNEXPECTED",last_alert=False,critical_latched=True,
    )
    with pytest.raises(m.V40GuardianRecovery005Halted,
                       match="EXISTING_SCOPE_STATE_INVALID"):
        asyncio.run(call(db,[]))
    assert db.inserts==[]


def test_all_seven_scope_ids_are_distinct_and_new_production_gate_pins_006():
    from app.t100_v40_guardian_post_gate import ACTIVE_REVIEWED_SCOPE as flat_gate
    from app.t100_v40_unreleased_nonflat_guardian_evidence import (
        ACTIVE_REVIEWED_SCOPE as nonflat_gate,
    )
    assert len({scope for scope,_ in m._PREDECESSORS}|{m.RECOVERY_SCOPE_005})==6
    from app.t100_v40_guardian_recovery_006 import RECOVERY_SCOPE_006
    assert RECOVERY_SCOPE_006 not in {scope for scope,_ in m._PREDECESSORS}
    assert RECOVERY_SCOPE_006 != m.RECOVERY_SCOPE_005
    assert flat_gate==nonflat_gate==RECOVERY_SCOPE_006
