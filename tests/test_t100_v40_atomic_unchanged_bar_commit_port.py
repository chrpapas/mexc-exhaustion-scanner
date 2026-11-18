"""One SQL transaction for 1..4 completed Min30 V40 unchanged-stop cursors.

The signed full-account observer, SELECT-only source, write-role pool and
fencing lease are simulated. No exchange POST and no DB connection.
"""
from dataclasses import replace
from decimal import Decimal as D
from types import SimpleNamespace

import pytest

from app.t100_live_db_inspect import EXPECTED_DATABASE
from app.t100_live_durable_recovery import checkpoint_from_row
from app.t100_live_four_slot_ledger_reader import (
    CHECKPOINTS_SQL,REVISIONS_SQL,INTENTS_SQL,AMENDMENTS_SQL,
    PostgresFourSlotLedgerReader,
)
from app.t100_live_four_slot_mexc_readonly import FourSlotMexcReadVenue
from app.t100_live_leader_fencing import LeaderLease
from app.t100_live_role_preflight import TARGET_ROLE
from app.t100_v40_live_portfolio_supervisor import V40CursorAdvanceResult
from app.t100_live_checkpoint_journal import BAR_OBSERVED,validate_checkpoint_transition
from app import t100_v40_unchanged_bar_commit_port as m
from tests.test_t100_v40_live_portfolio_supervisor import active
from tests.test_t100_v40_unreleased_verified_exit_handoff import bundle


class FakeTX:
    def __init__(self,db):
        self.db=db
    async def __aenter__(self):
        self.db.entered+=1
        return self
    async def __aexit__(self,typ,val,tb):
        self.db.exited+=1
        self.db.committed+=(typ is None)
        return False


class FakeAcquire:
    def __init__(self,db): self.db=db
    async def __aenter__(self):return self.db
    async def __aexit__(self,*args):pass


class FakeDb:
    def __init__(self,ledger,*,failure=None):
        self.ledger=ledger
        self.entered=0
        self.exited=0
        self.committed=0
        self.events=[]
        self.failure=failure
    def transaction(self,**kwargs):
        assert kwargs=={"isolation":"serializable"}
        return FakeTX(self)
    async def execute(self,sql):
        assert "pg_advisory_xact_lock" in sql
        self.events.append("LOCK")
    async def fetchrow(self,sql,*args):
        if "t100_live_executor_leases" in sql:
            return {"holder_id":"fenced-v40","fencing_token":27,
                    "unexpired":self.failure!="lease"}
        raise AssertionError("Unexpected row read")
    async def fetchval(self,sql,*args):
        if sql=="SELECT current_user":
            return TARGET_ROLE if self.failure!="role" else "untrusted"
        if sql=="SELECT current_database()":return EXPECTED_DATABASE
        if "transaction_read_only" in sql:return "off"
        if "clock_timestamp()-" in sql:
            return D("5") if self.failure=="stale" else D("0.25")
        if "count(*)" in sql.lower():return 1 if self.failure=="pending" else 0
        raise AssertionError("Unexpected scalar read")
    async def fetch(self,sql,*args):
        if sql==CHECKPOINTS_SQL:return self.ledger.checkpoints
        if sql==REVISIONS_SQL:return self.ledger.revisions
        if sql==INTENTS_SQL:return self.ledger.intents
        if sql==AMENDMENTS_SQL:return self.ledger.stop_amendments
        if "WHERE lifecycle_state<>'CLOSED'" in sql and "FOR UPDATE" in sql:
            return [{"position_id":x["position_id"],"symbol":x["symbol"]}
                    for x in self.ledger.checkpoints]
        raise AssertionError("Unexpected query")


class FakePool:
    def __init__(self,db):self.db=db
    def acquire(self):return FakeAcquire(self.db)


class FakeCursor:
    states={}
    actions=[]
    def __init__(self,conn):self.conn=conn
    async def read(self,position_id):return self.states[position_id]
    async def stage(self,*,cursor,previous,plan):
        assert cursor.position_id==previous.position_id
        assert plan.status=="KEEP_VERIFIED_NATIVE_STOP"
        self.actions.append(("STAGE",cursor.position_id))
        self.states[cursor.position_id]=replace(
            cursor,
            pending_open_ts=plan.proposed_checkpoint.last_completed_open_ts,
            pending_status=plan.status,
            pending_stop_price=plan.proposed_checkpoint.native_stop_price,
            pending_best_profit_pct=plan.proposed_checkpoint.best_profit_pct,
            pending_trail_armed=plan.proposed_checkpoint.trail_armed,
        )
    async def commit_verified(
        self,*,cursor,plan,persisted_checkpoint,
        independent_native_sl_get_verified,
        immutable_checkpoint_history_verified,
    ):
        assert independent_native_sl_get_verified
        assert immutable_checkpoint_history_verified
        assert cursor.pending_open_ts==plan.proposed_checkpoint.last_completed_open_ts
        self.actions.append(("COMMIT",cursor.position_id))
        updated=replace(
            cursor,
            pending_open_ts=None,pending_status=None,
            pending_stop_price=None,pending_best_profit_pct=None,
            pending_trail_armed=None,
            last_committed_open_ts=plan.proposed_checkpoint.last_completed_open_ts,
            checkpoint_version=persisted_checkpoint.checkpoint_version,
            best_profit_pct=persisted_checkpoint.best_profit_pct,
            trail_armed=persisted_checkpoint.armed,
            cursor_revision=cursor.cursor_revision+1,
        )
        self.states[cursor.position_id]=updated
        return updated


def environment(n,monkeypatch,*,failure=None,due_indices=None):
    frame=active(n,due_indices=due_indices)
    b=bundle(n)
    ledger=b["ledger"]
    venue=FourSlotMexcReadVenue.__new__(FourSlotMexcReadVenue)
    venue.last_positions=b["signed_account_positions"]
    venue.last_stops=b["signed_native_stops"]
    reader=PostgresFourSlotLedgerReader.__new__(PostgresFourSlotLedgerReader)
    reader.pool=object()
    db=FakeDb(ledger,failure=failure)
    pool=FakePool(db)
    readings=[]
    async def observe():
        readings.append("NEW_FULL_ACCOUNT_SIGNED_GET")
        return frame
    async def lease():
        readings.append("NEW_LEADER_FENCE")
        return LeaderLease("t100-live-executor","fenced-v40",27,45)
    writer=m.PostgresV40UnchangedBarCommit(
        writer_pool=pool,reader=reader,venue=venue,
        fresh_observe=observe,fresh_lease=lease,
    )
    FakeCursor.states={
        cp["position_id"]:c
        for cp,c in zip(ledger.checkpoints,b["cursors"])
    }
    FakeCursor.actions=[]
    writes=[]
    async def advance(conn,old,new,*,reason):
        validate_checkpoint_transition(old,new,reason=reason)
        writes.append((old,new,reason))
    monkeypatch.setattr(m,"V40DurableBarCursor",FakeCursor)
    monkeypatch.setattr(m,"_advance_checkpoint_in_transaction",advance)
    return frame,writer,db,readings,writes


@pytest.mark.asyncio
async def test_production_gate_fails_before_any_signed_get_or_sql(monkeypatch):
    frame,writer,db,readings,writes=environment(4,monkeypatch)
    with pytest.raises(
        m.V40UnchangedCursorCommitHalted,match="PERMIT_REQUIRED",
    ):
        await writer.commit(frame.completed_exit,permit=object())
    assert readings==[] and db.entered==0 and writes==[]


@pytest.mark.asyncio
@pytest.mark.parametrize("n",[1,2,3,4])
async def test_one_to_four_same_tx_stop_and_best_revisions(n,monkeypatch):
    frame,writer,db,readings,writes=environment(n,monkeypatch)
    monkeypatch.setattr(
        m,"require_certified_transport_permit",
        lambda x:x,
    )
    approval=SimpleNamespace(scope="T100_V40_FOUR_SLOT_PRODUCTION_TRIAL")
    out=await writer.commit(frame.completed_exit,permit=approval)
    assert type(out) is V40CursorAdvanceResult
    assert out.status=="V40_UNCHANGED_NATIVE_STOP_BAR_CURSOR_COMMITTED"
    assert out.durable_commit_verified and out.stop_bookends_verified
    assert out.exchange_posts==0
    assert len(out.position_ids)==n and len(set(out.position_ids))==n
    assert db.entered==1 and db.exited==1 and db.committed==1
    assert db.events==["LOCK"]
    assert len(writes)==n
    assert all(reason==BAR_OBSERVED and new.stop_price==old.stop_price
               and new.checkpoint_version==old.checkpoint_version+1
               for old,new,reason in writes)
    assert len(FakeCursor.actions)==2*n
    assert readings==[
        "NEW_FULL_ACCOUNT_SIGNED_GET","NEW_LEADER_FENCE",
    ]


@pytest.mark.asyncio
@pytest.mark.parametrize("failure,reason",[
    ("role","WRITER_ROLE_OR_DB_UNVERIFIED"),
    ("lease","WRITER_FENCE_STALE"),
    ("stale","SIGNED_GET_BOOKEND_TOO_OLD"),
    ("pending","OTHER_UNRESOLVED_MUTATIONS"),
])
async def test_guarded_writer_rolls_back_without_any_cursor_commit(
    monkeypatch,failure,reason,
):
    frame,writer,db,readings,writes=environment(4,monkeypatch,failure=failure)
    monkeypatch.setattr(m,"require_certified_transport_permit",lambda x:x)
    with pytest.raises(m.V40UnchangedCursorCommitHalted,match=reason):
        await writer.commit(
            frame.completed_exit,
            permit=SimpleNamespace(scope="T100_V40_FOUR_SLOT_PRODUCTION_TRIAL"),
        )
    assert db.entered==1 and db.committed==0
    assert writes==[] and FakeCursor.actions==[]


@pytest.mark.asyncio
async def test_current_due_projection_drift_blocks_before_write(monkeypatch):
    frame,writer,db,reads,writes=environment(3,monkeypatch)
    monkeypatch.setattr(m,"require_certified_transport_permit",lambda x:x)
    original=frame.completed_exit
    wrong=replace(original,protected_position_count=4)
    with pytest.raises(m.V40UnchangedCursorCommitHalted,match="CURRENT_SIGNED"):
        await writer.commit(
            wrong,
            permit=SimpleNamespace(scope="T100_V40_FOUR_SLOT_PRODUCTION_TRIAL"),
        )
    assert db.entered==0 and not writes


@pytest.mark.asyncio
async def test_original_canary_cannot_reuse_portfolio_cursor_writer(monkeypatch):
    frame,writer,db,reads,writes=environment(1,monkeypatch)
    monkeypatch.setattr(m,"require_certified_transport_permit",lambda x:x)
    with pytest.raises(
        m.V40UnchangedCursorCommitHalted,match="PERMIT_REQUIRED",
    ):
        await writer.commit(
            frame.completed_exit,
            permit=SimpleNamespace(scope="T100_V40_SUPERVISED_FIRST_SIGNAL"),
        )
    assert not reads and db.entered==0
