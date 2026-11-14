"""Atomic V40 close release uses positive native fills, funding and SQL fence."""
from dataclasses import replace,asdict
from datetime import datetime,timezone
from decimal import Decimal as D

import pytest

from app.t100_live_durable_recovery import PositionCheckpoint
from app.t100_live_position_accounting import (
    reconcile_verified_closed_position,
)
from app.t100_live_native_stop_close_recovery import NativeStopRecoveryReport
from app.t100_live_leader_fencing import LeaderLease
from app.t100_v40_unreleased_portfolio_native_close import V40PortfolioCloseWitness
from app.t100_v40_unreleased_verified_slot_release import (
    PostgresV40VerifiedSlotRelease,V40SlotReleaseHalted,
)
from tests.test_t100_live_position_accounting import (
    fill,fillset,provenance,funding,
)
from app.t100_live_role_preflight import TARGET_ROLE
from app.t100_live_db_inspect import EXPECTED_DATABASE


def positive():
    cp=PositionCheckpoint(
        symbol="SIREN_USDT",external_oid="t100v1eabc",
        exchange_order_id="100",position_id="10",contracts=D("2"),
        entry_price=D("100"),stop_order_id="99",stop_price=D("175"),
        best_profit_pct=D("0"),armed=False,
        lifecycle_state="OPEN",checkpoint_version=1,
    )
    entry=fillset("100","OPEN_SHORT",[
        fill("e1","100","OPEN_SHORT","2","100","0.1")],"100")
    close=fillset("200","CLOSE_SHORT",[
        fill("c1","200","CLOSE_SHORT","2","90","0.1")],"90")
    accounting=reconcile_verified_closed_position(
        contract_size=D("1"),entry_fills=entry,
        close_provenance=provenance(),close_fills=close,
        funding=funding(),
    )
    native=NativeStopRecoveryReport(
        "NATIVE_STOP_CLOSE_AND_OWNED_POSITION_VERIFIED_VENUE_TP_LABEL_CONFLICT",
        entry_filled_verified=True,native_stop_terminal_verified=True,
        linked_venue_close_filled=True,owned_position_closed_verified=True,
        venue_trigger_side_conflict=True,stop_loss_trigger_side_proven=False,
    )
    witness=V40PortfolioCloseWitness(
        "V40_PORTFOLIO_CLOSE_EXCHANGE_TP_LABEL_CONFLICT_UNRELEASED",
        closed_position_id="10",exact_close_order_id="200",
        surviving_protected_slots=0,closing_fills=close,
        native_terminal=native,exact_closed_position_history_verified=True,
        native_trigger_label_conflict=True,
        portfolio_unchanged_across_bookends=True,
    )
    lease=LeaderLease(
        "t100-live-executor","test-v40-holder",17,45,
    )
    return dict(
        witness=witness,previous=cp,accounting=accounting,lease=lease,
        observed_at=datetime.now(timezone.utc),
    )


class Context:
    def __init__(self,owner):
        self.owner=owner
    async def __aenter__(self):
        return self.owner
    async def __aexit__(self,exc_type,exc,tb):
        self.owner.exited_exception=exc_type
        return False


class DB:
    def __init__(self,inputs):
        self.inputs=inputs
        self.calls=[]
        self.exited_exception=None
        self.lease_ok=True
        self.accounting_ok=True
        self.checkpoint_ok=True
        self.pending=False
        self.fail_update=False
        self.role_ok=True
        self.successful_revisions=0
        self.intent_updates=0
        self.slot_updates=0

    def transaction(self,**kw):
        assert kw=={"isolation":"read_committed"}
        return Context(self)

    async def execute(self,sql,*args):
        self.calls.append(("execute",sql,args))
        assert "pg_advisory_xact_lock(10040, 31)" in sql

    async def fetchrow(self,sql,*args):
        self.calls.append(("fetchrow",sql,args))
        if "t100_live_executor_leases" in sql:
            return dict(holder_id=self.inputs["lease"].holder_id,
                        fencing_token=17,unexpired=self.lease_ok)
        if "FROM public.t100_live_position_checkpoints" in sql:
            d=asdict(self.inputs["previous"])
            if not self.checkpoint_ok:d["stop_price"]=D("176")
            return d
        if "FROM public.t100_live_v40_slot_reservations" in sql:
            return dict(signal_id=42,symbol="SIREN_USDT",
                        external_oid="t100v1eabc",state="PROTECTED")
        if "FROM public.t100_live_final_position_accounting" in sql:
            if not self.accounting_ok:return None
            a=self.inputs["accounting"]
            return dict(entry_order_id=a.entry_order_id,
                        close_order_id=a.close_order_id,
                        stop_order_id=a.stop_order_id,
                        opened_contracts=a.reconciliation.opened_contracts,
                        closed_contracts=a.reconciliation.closed_contracts,
                        realized_net_usdt=a.reconciliation.realized_net_usdt)
        raise AssertionError("unexpected fetchrow")

    async def fetch(self,sql,*args):
        self.calls.append(("fetch",sql,args))
        assert "FROM public.t100_live_position_checkpoints" in sql
        return [dict(position_id="10",symbol="SIREN_USDT")]

    async def fetchval(self,sql,*args):
        self.calls.append(("fetchval",sql,args))
        if sql=="SELECT current_user":
            return TARGET_ROLE if self.role_ok else "root"
        if sql=="SELECT current_database()":return EXPECTED_DATABASE
        if "EXTRACT(EPOCH" in sql:return D("0.2")
        if "COUNT(*)::int" in sql:return 1 if self.pending else 0
        if "UPDATE public.t100_live_position_checkpoints" in sql:
            if self.fail_update:raise ConnectionError("SECRET_DB_PASSWORD")
            return "10"
        if "INSERT INTO t100_live_position_checkpoint_revisions" in sql:
            self.successful_revisions+=1
            return self.successful_revisions
        if "UPDATE public.t100_live_order_intents" in sql:
            self.intent_updates+=1
            return 1
        if "UPDATE public.t100_live_v40_slot_reservations" in sql:
            self.slot_updates+=1
            return 42
        raise AssertionError("unexpected fetchval")


class Pool:
    def __init__(self,db):self.db=db
    def acquire(self):return Context(self.db)


def run(input=None,**changes):
    import asyncio
    evidence=input or positive()
    db=DB(evidence)
    for key,value in changes.items():
        setattr(db,key,value)
    async def execute():
        return await PostgresV40VerifiedSlotRelease(Pool(db)).close_and_release_once(
            **evidence,
        )
    return asyncio.run(execute()),db


def test_exact_native_close_and_accounting_released_atomically_but_requires_reobserve():
    r,db=run()
    assert r.status=="V40_CLOSE_DB_RELEASE_COMMITTED_RESTART_GET_REQUIRED"
    assert r.position_id=="10"
    assert r.durable_release_and_close_committed
    assert not r.next_entry_allowed and not r.live_authorization
    assert r.mexc_posts==0
    assert r.post_commit_signed_get_required
    assert r.fresh_guardian_and_margin_recheck_required
    assert db.successful_revisions==2
    assert db.intent_updates==1 and db.slot_updates==1
    assert db.exited_exception is None
    calls=db.calls
    assert "pg_advisory_xact_lock" in calls[0][1]
    # CLOSING and CLOSED were both journaled without a partial commit.
    assert sum("UPDATE public.t100_live_position_checkpoints" in x[1] for x in calls)==2


@pytest.mark.parametrize("condition",[
    {"lease_ok":False},
    {"accounting_ok":False},
    {"checkpoint_ok":False},
    {"pending":True},
    {"role_ok":False},
])
def test_stale_fence_absent_accounting_unknown_attempt_or_owner_blocks_before_writes(condition):
    with pytest.raises(V40SlotReleaseHalted):
        run(**condition)


def test_lost_postgres_commit_ack_is_unknown_no_retry():
    with pytest.raises(V40SlotReleaseHalted,match="SQL_OUTCOME_UNKNOWN"):
        run(fail_update=True)


@pytest.mark.parametrize("bad",[
    "no_finality","partial","bad_fee","wrong_order","missing_funding",
    "stale_checkpoint","untrusted_accounting","no_stop_terminal",
    "manual_position_outside_portfolio",
])
def test_unproven_exit_cannot_release_or_modify_any_slot(bad):
    x=positive()
    if bad=="no_finality":
        x["witness"]=replace(x["witness"],
            exact_closed_position_history_verified=False)
    if bad=="partial":
        x["witness"]=replace(x["witness"],
            closing_fills=replace(x["witness"].closing_fills,
                                  deal_volume=D("1")))
    if bad=="bad_fee":
        x["witness"]=replace(x["witness"],closing_fills=replace(
            x["witness"].closing_fills,strategy_fully_filled_order_complete=False))
    if bad=="wrong_order":
        x["accounting"]=replace(x["accounting"],close_order_id="300")
    if bad=="missing_funding":
        x["accounting"]=replace(x["accounting"],funding_history_complete=False)
    if bad=="stale_checkpoint":
        x["previous"]=replace(x["previous"],lifecycle_state="CLOSING")
    if bad=="untrusted_accounting":
        x["accounting"]=object()
    if bad=="no_stop_terminal":
        x["witness"]=replace(x["witness"],native_terminal=replace(
            x["witness"].native_terminal,native_stop_terminal_verified=False))
    if bad=="manual_position_outside_portfolio":
        x["witness"]=replace(x["witness"],portfolio_unchanged_across_bookends=False)
    with pytest.raises(V40SlotReleaseHalted):
        run(input=x)
