"""Close accounting never runs before exact flat signed GET/fill proof."""
import asyncio
from dataclasses import replace

import pytest

import app.t100_v40_emergency_completion as m
from app.t100_live_durable_recovery import checkpoint_from_row
from tests.test_t100_v40_emergency_close import target,checkpoint
from tests.test_t100_v40_emergency_postflight import run as postflight


class FakePool:pass


def fixture(monkeypatch,*,state="OPEN",fills="PERSISTED",release=True,fail=False):
    events=[]
    writer=m.PostgresV40EmergencyCompletion(FakePool())
    cp=checkpoint_from_row(checkpoint())
    if state=="CLOSING":
        cp=replace(cp,lifecycle_state=state,checkpoint_version=2)
    if state=="CLOSED":
        cp=replace(cp,lifecycle_state=state,checkpoint_version=3)

    async def read(_):
        events.append("EXACT_ORIGINAL_CLAIM_DB")
        return cp

    async def do_release(_):
        events.append("RELEASE_ONE_PROTECTED_SLOT")
        return release

    async def persist(_):
        events.append("IMMUTABLE_CLOSE_DEALS_PERSIST")
        if fail:raise TimeoutError("SECRET_DB_DSN")
        return fills

    class Ledger:
        def __init__(self,p):pass
        async def persist(self,fill_set):
            assert fill_set.side=="CLOSE_SHORT"
            return await persist(fill_set)

    class Journal:
        def __init__(self,p):pass
        async def advance(self,prev,new,*,reason):
            events.append((prev.lifecycle_state,new.lifecycle_state,reason))
            assert prev.external_oid==target().entry_external_oid
            assert prev.position_id==target().position_id

    writer._read=read
    writer._release=do_release
    monkeypatch.setattr(m,"PostgresVerifiedFillLedger",Ledger)
    monkeypatch.setattr(m,"PostgresCheckpointJournal",Journal)
    return writer,events


def run(writer,evidence=None):
    return asyncio.run(writer.finalize_verified(
        target=target(),
        postflight=postflight() if evidence is None else evidence,
    ))


@pytest.mark.parametrize("state,expected",[
    ("OPEN",2),("CLOSING",1),("CLOSED",0),
])
def test_can_resume_closing_after_crash_without_any_exchange_post(monkeypatch,state,expected):
    writer,events=fixture(monkeypatch,state=state)
    result=run(writer)
    assert result.status=="V40_EMERGENCY_CLOSE_CHECKPOINT_INTENT_AND_SLOT_CLOSED_VERIFIED"
    assert result.immutable_fill_ledger_verified
    assert result.original_checkpoint_closed
    assert result.original_intent_closed
    assert result.first_slot_released
    assert result.exchange_posts==0
    assert not result.further_entries_allowed
    assert not result.further_close_post_allowed
    transitions=[x for x in events if type(x) is tuple]
    assert len(transitions)==expected
    if state=="OPEN":
        assert transitions==[
            ("OPEN","CLOSING","CLOSING_LATCHED"),
            ("CLOSING","CLOSED","CLOSE_VERIFIED"),
        ]
    assert events[0]=="EXACT_ORIGINAL_CLAIM_DB"
    assert events[1]=="IMMUTABLE_CLOSE_DEALS_PERSIST"
    assert events[-1]=="RELEASE_ONE_PROTECTED_SLOT"


def test_no_signatures_or_no_flat_proof_never_writes_any_db(monkeypatch):
    writer,events=fixture(monkeypatch)
    bad=replace(postflight(),full_account_flat_double_get_verified=False)
    with pytest.raises(m.V40EmergencyCompletionHalted,match="SIGNED_FLAT"):
        run(writer,bad)
    assert events==[]


def test_fill_persistence_timeout_freezes_before_checkpoint_transitions(monkeypatch):
    writer,events=fixture(monkeypatch,fail=True)
    with pytest.raises(m.V40EmergencyCompletionHalted,match="IMMUTABLE_FILLS"):
        run(writer)
    assert events==[
        "EXACT_ORIGINAL_CLAIM_DB","IMMUTABLE_CLOSE_DEALS_PERSIST",
    ]


def test_unknown_checkpoint_commit_or_slot_release_requires_db_reconcile(monkeypatch):
    writer,events=fixture(monkeypatch,release=False)
    with pytest.raises(m.V40EmergencyCompletionHalted,match="DB_COMMIT_UNKNOWN"):
        run(writer)
    assert events.count("RELEASE_ONE_PROTECTED_SLOT")==1
    assert "IMMUTABLE_CLOSE_DEALS_PERSIST" in events
    assert not any("POST" in str(x) for x in events)
