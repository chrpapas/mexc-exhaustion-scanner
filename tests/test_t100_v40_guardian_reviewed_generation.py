"""Safety tests for one-time V40 incident-preserving reviewed generation."""
import asyncio
import pytest

import app.t100_v40_guardian_reviewed_generation as m
from tests.test_t100_v40_canary_durable_guardian import env,flat,exposed


class DB:
    def __init__(self):
        self.reviewed=None
        self.legacy={
            "last_status":m.GOOD_STATUS,
            "last_alert":False,
            "critical_latched":True,
            "revision":12,
            "recently_healthy":True,
        }
        self.incidents={
            "critical_total":1,
            "forbidden_critical":0,
            "undelivered":0,
        }
        self.queries=[]
        self.saved=[]
    async def fetchrow(self,sql,*args):
        self.queries.append(sql.lstrip().split(" ",1)[0])
        if "WHERE scope_sha256=$1" in sql and (
            "FROM public.t100_guardian_incident_state" in sql
        ):
            if args[0]==m.REVIEWED_SCOPE:return self.reviewed
            assert args[0]==m.LEGACY_SCOPE
            return self.legacy
        if "FROM public.t100_guardian_incident_outbox" in sql:
            assert args==(m.LEGACY_SCOPE,m.HISTORIC_CRITICAL)
            return self.incidents
        if "INSERT INTO public.t100_guardian_incident_state" in sql:
            assert args[0]==m.REVIEWED_SCOPE
            assert args[1]==m.AUDIT_STATUS and args[2] is False
            self.saved.append(tuple(args))
            return {"revision":1,"critical_latched":False}
        raise AssertionError("unexpected SQL")
    async def fetch(self,*args):
        return []
    async def fetchval(self,*args):
        return 0


async def select(db, items=None):
    samples=list(items or [flat(),flat()])
    calls=[]
    async def observe(_):
        calls.append("READ_ONLY_ACCOUNT_GET")
        v=samples.pop(0)
        if isinstance(v,Exception):raise v
        return v
    async def pause(seconds):
        assert seconds==3.0
        calls.append("PAUSE")
    result=await m.select_reviewed_scope(
        db,env=env(),observe=observe,pause=pause,
    )
    return result,calls


def test_reviewed_generation_keeps_old_critical_latch_and_outbox_unmodified():
    db=DB()
    result,calls=asyncio.run(select(db))
    assert result==(m.REVIEWED_SCOPE,
                    "NEW_REVIEWED_GENERATION_AFTER_DOUBLE_GET")
    assert calls==[
        "READ_ONLY_ACCOUNT_GET","PAUSE","READ_ONLY_ACCOUNT_GET"
    ]
    assert len(db.saved)==1
    assert db.legacy["critical_latched"] is True
    assert db.incidents["critical_total"]==1
    assert "UPDATE" not in db.queries and "DELETE" not in db.queries


@pytest.mark.parametrize("fault",[
    {"last_status":"OBSERVER_V40_CANARY_SIGNED_GET_OR_ACCOUNT_INVALID"},
    {"last_alert":True},
    {"critical_latched":False},
    {"recently_healthy":False},
    {"revision":1},
])
def test_legacy_must_be_recently_healthy_still_latched(fault):
    db=DB()
    db.legacy.update(fault)
    with pytest.raises(m.V40GuardianReviewedGenerationHalted,
                       match="LEGACY_NOT_RECOVERED"):
        asyncio.run(select(db))
    assert db.saved==[]


@pytest.mark.parametrize("fault",[
    {"critical_total":0},
    {"critical_total":2},
    {"forbidden_critical":1},
    {"undelivered":1},
])
def test_any_unreviewed_or_undelivered_critical_blocks_rollover(fault):
    db=DB()
    db.incidents.update(fault)
    with pytest.raises(m.V40GuardianReviewedGenerationHalted,
                       match="UNREVIEWED_ALERTS"):
        asyncio.run(select(db))
    assert db.saved==[]


@pytest.mark.parametrize("bad",[
    exposed(),
    RuntimeError("MEXC_PRIVATE_KEY_TOKEN"),
])
def test_fresh_unknown_position_or_get_error_never_creates_new_generation(bad):
    db=DB()
    with pytest.raises(m.V40GuardianReviewedGenerationHalted,
                       match="FRESH_FLAT_UNVERIFIED") as e:
        asyncio.run(select(db,[flat(),bad]))
    assert "MEXC_PRIVATE_KEY_TOKEN" not in str(e.value)
    assert db.saved==[]


def test_restart_reuses_existing_reviewed_latch_without_clearing_it():
    db=DB()
    db.reviewed={
        "last_status":"OBSERVER_V40_CANARY_SIGNED_GET_OR_ACCOUNT_INVALID",
        "last_alert":True,
        "critical_latched":True,
    }
    result,calls=asyncio.run(select(db))
    assert result==(m.REVIEWED_SCOPE,"EXISTING_REVIEWED_GENERATION")
    assert calls==[] and db.saved==[]


def test_unknown_reviewed_state_fails_closed():
    db=DB()
    db.reviewed={"last_status":"UNRECOGNIZED_STATUS",
                 "last_alert":False,"critical_latched":False}
    with pytest.raises(m.V40GuardianReviewedGenerationHalted,
                       match="REVIEWED_STATE_INVALID"):
        asyncio.run(select(db))
    assert db.saved==[]


def test_audited_scope_differs_from_legacy_fixed_hash():
    assert len(m.LEGACY_SCOPE)==len(m.REVIEWED_SCOPE)==64
    assert m.LEGACY_SCOPE != m.REVIEWED_SCOPE
    assert m.AUDIT_STATUS.startswith("OBSERVER_")
