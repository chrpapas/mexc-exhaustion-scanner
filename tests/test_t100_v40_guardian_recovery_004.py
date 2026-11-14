"""Audited one-time guardian recovery 004 preserves all older criticals."""
import asyncio
from dataclasses import replace

import pytest

from app import t100_v40_guardian_recovery_004 as m
from tests.test_t100_v40_canary_durable_guardian import flat,exposed


class DB:
    def __init__(self):
        self.states={
            scope:dict(
                last_status=m.GOOD_STATUS,last_alert=False,critical_latched=True,
                revision=180,recently_healthy=True,
            ) for scope,_ in m._OLD_SCOPES
        }
        self.states[m.RECOVERY_SCOPE_004]=None
        self.outboxes={
            scope:dict(
                total=n,observer_class=n,other_critical=0,pending=0,
            ) for scope,n in m._OLD_SCOPES
        }
        self.old_states_before={
            k:v.copy() for k,v in self.states.items() if v is not None
        }
        self.inserts=[]
        self.calls=[]

    async def fetchrow(self,sql,*args):
        self.calls.append((sql,args))
        if "INSERT INTO public.t100_guardian_incident_state" in sql:
            assert args==(m.RECOVERY_SCOPE_004,m.AUDIT_STATUS,False)
            self.inserts.append(args)
            return dict(revision=1,critical_latched=False)
        if "FROM public.t100_guardian_incident_state" in sql:
            return self.states.get(args[0])
        if "FROM public.t100_guardian_incident_outbox" in sql:
            assert args[1]==m.HISTORIC_CRITICAL
            return self.outboxes[args[0]]
        raise AssertionError("Unexpected SQL")
    async def fetch(self,*args):return []
    async def fetchval(self,*args):return 0


async def call(db, snapshots=None):
    readings=iter(
        snapshots if snapshots is not None else (
            replace(flat(),ledger_provenance_verified=True),
            replace(flat(),ledger_provenance_verified=True),
        )
    )
    observed=[]
    waited=[]
    async def observe(_):
        observed.append("real_signed_GET_and_independent_ledger")
        item=next(readings)
        if isinstance(item,Exception):raise item
        return item
    async def pause(seconds):
        waited.append(seconds)
    result=await m.select_fourth_reviewed_scope(
        db,env={},observe=observe,pause=pause,
    )
    return result,observed,waited


def test_exact_four_generations_and_three_003_critical_deliveries_only():
    db=DB()
    result,observed,waited=asyncio.run(call(db))
    assert result[0]==m.RECOVERY_SCOPE_004
    assert result[1]=="NEW_RECOVERY_004_AFTER_FOUR_GENERATIONS_AND_DOUBLE_SIGNED_GET"
    assert observed==["real_signed_GET_and_independent_ledger"]*2
    assert waited==[3.0]
    assert len(db.inserts)==1
    assert all(
        db.states[scope]==db.old_states_before[scope]
        for scope,_ in m._OLD_SCOPES
    )
    assert all(
        "DELETE" not in sql.upper()
        and "UPDATE" not in sql.upper()
        for sql,_ in db.calls
    )


@pytest.mark.parametrize("idx,field,bad",[
    (0,"total",2),(1,"observer_class",0),
    (2,"pending",1),(3,"total",2),
    (3,"observer_class",2),(3,"other_critical",1),(3,"pending",1),
])
def test_mismatched_incident_history_no_new_scope(idx,field,bad):
    db=DB()
    scope=m._OLD_SCOPES[idx][0]
    db.outboxes[scope][field]=bad
    with pytest.raises(m.V40GuardianRecovery004Halted,
                       match="INCIDENT_MISMATCH"):
        asyncio.run(call(db))
    assert not db.inserts


@pytest.mark.parametrize("idx,field,bad",[
    (0,"critical_latched",False),
    (1,"last_alert",True),
    (2,"last_status","OBSERVER_UNEXPECTED"),
    (3,"recently_healthy",False),
    (3,"revision",0),
    (3,"critical_latched",False),
])
def test_mismatched_predecessor_state_no_recovery(idx,field,bad):
    db=DB()
    db.states[m._OLD_SCOPES[idx][0]][field]=bad
    with pytest.raises(m.V40GuardianRecovery004Halted,
                       match="STATE_NOT_REVIEWED"):
        asyncio.run(call(db))
    assert not db.inserts


@pytest.mark.parametrize("bad",[
    exposed(),
    flat(),
    replace(flat(),ledger_provenance_verified=False),
    replace(flat(),inventory_two_signed_get_bookends=False),
    replace(flat(),hedge_mode_verified=False),
    replace(flat(),monitored_account_flat=False),
    RuntimeError("SECRET"),
])
def test_unsafe_or_incomplete_second_observation_denies_recovery(bad):
    db=DB()
    with pytest.raises(m.V40GuardianRecovery004Halted,
                       match="FRESH_ACCOUNT_GET_OR_LEDGER_UNVERIFIED"):
        asyncio.run(call(db,[
            replace(flat(),ledger_provenance_verified=True),bad,
        ]))
    assert not db.inserts


def test_existing_004_latched_never_reinitializes_even_if_flat():
    db=DB()
    db.states[m.RECOVERY_SCOPE_004]=dict(
        last_status=m.GOOD_STATUS,last_alert=False,critical_latched=True
    )
    result,observed,waited=asyncio.run(call(db,[]))
    assert result==(m.RECOVERY_SCOPE_004,
                    "EXISTING_RECOVERY_004_SCOPE_STICKY")
    assert not observed and not waited and not db.inserts


def test_existing_004_unknown_status_halts():
    db=DB()
    db.states[m.RECOVERY_SCOPE_004]=dict(
        last_status="UNEXPECTED",last_alert=False,critical_latched=True
    )
    with pytest.raises(m.V40GuardianRecovery004Halted,
                       match="EXISTING_SCOPE_STATE_INVALID"):
        asyncio.run(call(db,[]))
    assert not db.inserts


def test_scope_id_distinct_and_all_old_scopes_preserved():
    assert len({m.RECOVERY_SCOPE_004,
                *(scope for scope,_ in m._OLD_SCOPES)})==5
    assert len(m.RECOVERY_SCOPE_004)==64
