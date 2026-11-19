"""Exact 005 HTTP-5xx incident reviewed rollover: no previous latch reset.

Offline only: fake PostgreSQL rows, fake signed flat/hedge/ledger GETs.
Wrong incident count, time, class, pending Discord, stale 005, hidden
nonflat, and any observer failure cannot initialize the new scope.
"""
from __future__ import annotations

from dataclasses import replace
import pytest

from app import t100_v40_guardian_recovery_006 as m
from tests.test_t100_v40_canary_durable_guardian import flat


class DB:
    def __init__(self):
        self.states={
            scope:dict(
                last_status=m.GOOD_STATUS,last_alert=False,
                critical_latched=True,revision=200,recently_healthy=True,
            )
            for scope,_ in m._OLD_SCOPES
        }
        self.states[m.RECOVERY_SCOPE_006]=None
        self.outboxes={
            scope:dict(
                total=n,observer_critical=n,other_critical=0,pending=0,
                nov19_incident_in_window=1 if scope==m.RECOVERY_SCOPE_005 else 0,
                nov15_window=1 if scope==m.RECOVERY_SCOPE_004 else 0,
                nov16_window=1 if scope==m.RECOVERY_SCOPE_004 else 0,
                nov17_window=1 if scope==m.RECOVERY_SCOPE_004 else 0,
            ) for scope,n in m._OLD_SCOPES
        }
        self.calls=[]
        self.inserted=[]
    async def fetchrow(self,sql,*args):
        self.calls.append((sql,args))
        if "INSERT INTO public.t100_guardian_incident_state" in sql:
            assert args==(m.RECOVERY_SCOPE_006,m.AUDIT_STATUS,False)
            self.inserted.append(args)
            return dict(revision=1,critical_latched=False)
        if "FROM public.t100_guardian_incident_state" in sql:
            return self.states.get(args[0])
        if "FROM public.t100_guardian_incident_outbox" in sql:
            assert args[1]==m.HISTORIC_CRITICAL
            return self.outboxes[args[0]]
        raise AssertionError("unexpected SQL")
    async def fetchval(self,*args):return 0
    async def fetch(self,*args):return []


def env():
    return {
        "T100_V40_GUARDIAN_AUDITED_ROLLOVER":"true",
        "T100_V40_GUARDIAN_RECOVERY_005_OPERATOR_APPROVED":"true",
        "T100_V40_GUARDIAN_RECOVERY_006_OPERATOR_APPROVED":"true",
    }


async def run(db,*,readings=None,e=None):
    events=iter(readings if readings is not None else [
        replace(flat(),ledger_provenance_verified=True),
        replace(flat(),ledger_provenance_verified=True),
    ])
    observed=[]
    pauses=[]
    async def observer(_env):
        observed.append("FRESH_SIGNED_FLAT_LEDGER")
        x=next(events)
        if isinstance(x,BaseException):raise x
        return x
    async def pause(s):
        pauses.append(s)
    result=await m.select_sixth_reviewed_scope(
        db,env=e if e is not None else env(),
        observe=observer,pause=pause,
    )
    return result,observed,pauses


@pytest.mark.asyncio
async def test_exact_one_http5xx_delivered_and_fresh_two_get_initializes_only_006():
    db=DB()
    old={k:v.copy() for k,v in db.states.items() if v is not None}
    result,reads,pauses=await run(db)
    assert result[0]==m.RECOVERY_SCOPE_006
    assert result[1].startswith("NEW_RECOVERY_006")
    assert reads==["FRESH_SIGNED_FLAT_LEDGER"]*2
    assert pauses==[3.0]
    assert len(db.inserted)==1
    for k,v in old.items():
        assert db.states[k]==v
    assert not any("UPDATE" in sql or "DELETE" in sql
                   for sql,args in db.calls)


@pytest.mark.asyncio
async def test_sticky_existing_006_never_reinitializes_even_after_critical():
    db=DB()
    db.states[m.RECOVERY_SCOPE_006]=dict(
        last_status=m.HISTORIC_CRITICAL,last_alert=False,critical_latched=True,
    )
    result,reads,pauses=await run(db)
    assert result==(m.RECOVERY_SCOPE_006,"EXISTING_RECOVERY_006_SCOPE_STICKY")
    assert reads==[] and pauses==[] and db.inserted==[]


@pytest.mark.asyncio
@pytest.mark.parametrize("fault",[
    "other_critical","extra_http_5xx","wrong_critical_class",
    "wrong_timestamp","pending_discord","stale_005",
    "unlatched_005","wrong_old_archive","old_timestamps_drift",
    "old_incident","old_pending","missing_005","without_permission",
    "without_prior_005_permission","nonflat","signed_get_exception",
    "uncertain_ledger",
])
async def test_unreviewed_history_or_signed_get_refuses_new_006(fault):
    db=DB()
    opts=env()
    beat=replace(flat(),ledger_provenance_verified=True)
    reads=[beat,beat]
    if fault=="other_critical":
        db.outboxes[m.RECOVERY_SCOPE_005]["other_critical"]=1
    elif fault=="extra_http_5xx":
        db.outboxes[m.RECOVERY_SCOPE_005]["total"]=2
    elif fault=="wrong_critical_class":
        db.outboxes[m.RECOVERY_SCOPE_005]["observer_critical"]=0
    elif fault=="wrong_timestamp":
        db.outboxes[m.RECOVERY_SCOPE_005]["nov19_incident_in_window"]=0
    elif fault=="pending_discord":
        db.outboxes[m.RECOVERY_SCOPE_005]["pending"]=1
    elif fault=="stale_005":
        db.states[m.RECOVERY_SCOPE_005]["recently_healthy"]=False
    elif fault=="unlatched_005":
        db.states[m.RECOVERY_SCOPE_005]["critical_latched"]=False
    elif fault=="wrong_old_archive":
        db.outboxes[m._OLD_SCOPES[0][0]]["total"]=2
    elif fault=="old_timestamps_drift":
        db.outboxes[m.RECOVERY_SCOPE_004]["nov16_window"]=0
    elif fault=="old_incident":
        db.outboxes[m.RECOVERY_SCOPE_004]["other_critical"]=1
    elif fault=="old_pending":
        db.outboxes[m.RECOVERY_SCOPE_004]["pending"]=1
    elif fault=="missing_005":
        db.states[m.RECOVERY_SCOPE_005]=None
    elif fault=="without_permission":
        opts["T100_V40_GUARDIAN_RECOVERY_006_OPERATOR_APPROVED"]="false"
    elif fault=="without_prior_005_permission":
        opts["T100_V40_GUARDIAN_RECOVERY_005_OPERATOR_APPROVED"]="false"
    elif fault=="nonflat":
        reads[1]=replace(beat,monitored_account_flat=False)
    elif fault=="signed_get_exception":
        reads[1]=TimeoutError("SENSITIVE_EXCHANGE_ERROR")
    elif fault=="uncertain_ledger":
        reads[1]=replace(beat,ledger_provenance_verified=False)
    with pytest.raises(m.V40GuardianRecovery006Halted):
        await run(db,readings=reads,e=opts)
    assert db.inserted==[]


@pytest.mark.asyncio
async def test_new_scope_db_write_unknown_never_silently_retries():
    db=DB()
    async def broken(sql,*args):
        if "INSERT INTO public.t100_guardian_incident_state" in sql:
            raise ConnectionError("DB credentials")
        return await DB.fetchrow(db,sql,*args)
    db.fetchrow=broken
    with pytest.raises(
        m.V40GuardianRecovery006Halted,
        match="DB_OR_LIVE_ACCOUNT_REVIEW_UNAVAILABLE",
    ) as exc:
        await run(db)
    assert "credentials" not in str(exc.value)
    assert db.inserted==[]
