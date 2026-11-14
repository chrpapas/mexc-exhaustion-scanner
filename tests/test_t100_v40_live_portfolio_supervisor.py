"""V40 four-slot supervisor integration fault matrix (no real POST / DB)."""
import asyncio
from dataclasses import replace
from datetime import datetime, timezone

import pytest

from app.t100_live_execution import LiveCaps
from app.t100_live_v40_entry_coordinator import V40OneShotReport
from app.t100_live_v40_stage2_source import V40SourceBatch, inspect_stage2_candidate
from app.t100_v40_guardian_post_gate import V40GuardianPostGateEvidence
from app.t100_v40_unreleased_nonflat_guardian_evidence import V40NonflatGuardianEvidence
from app.t100_v40_unreleased_native_stop_cycle import V40UnreleasedStopCycleResult
from app.t100_v40_unreleased_portfolio_native_close import V40PortfolioCloseWitness
from app.t100_v40_live_portfolio_supervisor import (
    V40CursorAdvanceResult, V40PortfolioCycleFrame, V40PortfolioSupervisor,
    run_v40_portfolio_supervisor,
)
from app.t100_live_four_slot_snapshot_coordinator import FourSlotSnapshotCoordinator
from tests.test_t100_live_four_slot_snapshot_coordinator import (
    fixture, Ledger, Venue, Completeness, report,
)
from tests.test_t100_live_v40_stage2_source import fixtures, T
from tests.test_t100_v40_unreleased_verified_exit_handoff import (
    bundle,
)
from app.t100_v40_unreleased_verified_exit_handoff import project_verified_owned_v40_exits
from decimal import Decimal as D


async def clean_flat():
    ld,orders,positions,stops=fixture(0)
    return await FourSlotSnapshotCoordinator(
        ledger=Ledger(ld),venue=Venue(orders,positions,stops),
        completeness=Completeness(report(0)),
    ).inspect()


def source(with_candidate=True):
    if with_candidate:
        sig,hist=fixtures()
        candidate=inspect_stage2_candidate(sig,hist,observed_at=T)
        return V40SourceBatch(
            "FROZEN_V40_STAGE2_SOURCE_VALIDATED_NONAUTHORIZING",
            observed_at=T,candidates=(candidate,),
        )
    return V40SourceBatch(
        "FROZEN_V40_STAGE2_SOURCE_VALIDATED_NONAUTHORIZING",
        observed_at=T,candidates=(),
    )


def flat_guardian():
    return V40GuardianPostGateEvidence(
        "V40_GUARDIAN_POST_GATE_CURRENT_FLAT_UNLATCHED_NONAUTHORIZING",
        state_verified=True,outbox_verified=True,guardian_fresh=True,
        critical_latch_absent=True,
    )


def owned_guardian():
    return V40NonflatGuardianEvidence(
        "V40_NONFLAT_GUARDIAN_PROTECTED_OBSERVER_FRESH_NONAUTHORIZING",
        guardian_protected_observation_fresh=True,
        no_current_scope_critical_or_pending_incidents=True,
    )


async def flat(with_candidate=True):
    return V40PortfolioCycleFrame(
        await clean_flat(),flat_guardian(),source(with_candidate),None,
    )


def active(n=4, *,low="95",high="100",with_candidate=True):
    d=bundle(n)
    cp=d["ledger"].checkpoints[0]
    sym=cp["symbol"]
    d["completed_bars_by_symbol"][sym]=replace(
        d["completed_bars_by_symbol"][sym],
        low=D(low),high=D(high),close=min(D(high),D("96")),
    )
    if D(low)<D("96"):
        d["completed_bars_by_symbol"][sym]=replace(
            d["completed_bars_by_symbol"][sym],close=D(low),
        )
    projection=project_verified_owned_v40_exits(**d)
    return V40PortfolioCycleFrame(
        d["snapshot"],owned_guardian(),source(with_candidate),projection,
    )


class Ports:
    def __init__(self,frame):
        self.frame=frame
        self.calls=[]
        self.entry_report=V40OneShotReport(
            "V40_ONE_PROTECTED_ENTRY_RECOVERED_NOT_AUTONOMOUS",
            signal_id=42,reserved=True,staged=True,post_attempted=True,
            claim_may_exist=True,independently_recovered_after_commit=True,
        )
    async def observe(self):
        self.calls.append("get")
        return self.frame
    async def enter_one(self,**kwargs):
        self.calls.append("entry")
        return self.entry_report
    async def tighten_one_stop(self,*args,**kwargs):
        self.calls.append("stop")
        return V40UnreleasedStopCycleResult(
            "V40_TRAIL_ONE_STOP_POST_ATTEMPT_RECONCILE_ONLY",
            stop_post_may_have_occurred=True,requires_independent_reconciliation=True,
        )
    async def persist_unchanged_stop_checkpoint(self,projection):
        self.calls.append("cursor")
        return V40CursorAdvanceResult(
            "V40_UNCHANGED_NATIVE_STOP_BAR_CURSOR_COMMITTED",
            tuple(s.position_id for s in projection.batch.slots),
            durable_commit_verified=True,stop_bookends_verified=True,
        )
    async def reconcile_native_close(self,projection):
        self.calls.append("close")
        return V40PortfolioCloseWitness("V40_NATIVE_STOP_CLOSE_UNVERIFIED")


def controller(p):
    return V40PortfolioSupervisor(
        p,caps=LiveCaps(D("12.50"),D("50"),D("50")),
        parity_report={},holder_id="v40-live-supervisor-test",
    )


@pytest.mark.asyncio
async def test_flat_without_signal_is_monitor_only():
    p=Ports(await flat(False))
    out=await controller(p).run_one()
    assert out.status=="V40_SUPERVISOR_WAIT_GENUINE_STAGE2"
    assert p.calls==["get"]
    assert not out.live_authorization


@pytest.mark.asyncio
async def test_genuine_signal_does_not_reuse_first_canary_permit_or_post():
    p=Ports(await flat())
    out=await controller(p).run_one()
    assert out.status=="V40_SUPERVISOR_ENTRY_PERMIT_UNAVAILABLE"
    assert out.signal_id==42
    assert p.calls==["get"]
    assert not out.origin_may_have_mutated


@pytest.mark.asyncio
async def test_unchanged_stop_commits_only_durable_bar_then_reobserves():
    p=Ports(active(3))
    out=await controller(p).run_one()
    assert out.status=="V40_SUPERVISOR_BAR_COMMITTED_REOBSERVE"
    assert out.observed_slots==3
    assert p.calls==["get","cursor"]


@pytest.mark.asyncio
async def test_four_slots_at_capacity_do_not_start_new_order():
    p=Ports(active(4))
    first=await controller(p).run_one()
    assert first.status=="V40_SUPERVISOR_BAR_COMMITTED_REOBSERVE"
    p.frame=replace(p.frame,completed_exit=None,
                    no_bar_due_ids=tuple(x.position_id for x in p.frame.snapshot.recovery.recovered))
    second=await controller(p).run_one()
    assert second.status=="V40_SUPERVISOR_FOUR_SLOTS_OCCUPIED"
    assert p.calls==["get","cursor","get"]


@pytest.mark.asyncio
async def test_stop_touch_preempts_candidate_and_never_releases_slot():
    p=Ports(active(4,high="180",low="80"))
    out=await controller(p).run_one()
    assert out.status=="V40_SUPERVISOR_NATIVE_EXIT_RECONCILE_DURABLE_RELEASE_REQUIRED"
    assert p.calls==["get","close"]
    assert not out.entries_allowed


@pytest.mark.asyncio
async def test_trailing_amendment_not_posted_without_distinct_portfolio_permit():
    p=Ports(active(2,low="86"))
    out=await controller(p).run_one()
    assert out.status=="V40_SUPERVISOR_STOP_PERMIT_UNAVAILABLE"
    assert p.calls==["get"]


@pytest.mark.asyncio
async def test_stop_post_unknown_is_never_retried(monkeypatch):
    p=Ports(active(2,low="86"))
    monkeypatch.setattr(V40PortfolioSupervisor,"_permit_valid",staticmethod(lambda x:True))
    out=await controller(p).run_one(permit=object())
    assert out.status=="V40_SUPERVISOR_STOP_GET_AND_CHECKPOINT_REQUIRED"
    assert out.must_reconcile_before_next_cycle
    assert p.calls==["get","stop"]


@pytest.mark.asyncio
async def test_entry_success_requires_new_account_observation(monkeypatch):
    p=Ports(await flat())
    monkeypatch.setattr(V40PortfolioSupervisor,"_permit_valid",staticmethod(lambda x:True))
    out=await controller(p).run_one(permit=object())
    assert out.status=="V40_SUPERVISOR_ONE_PROTECTED_ENTRY_REOBSERVE"
    assert p.calls==["get","entry"]
    assert out.origin_may_have_mutated


@pytest.mark.asyncio
async def test_ambiguous_order_rejects_additional_entry(monkeypatch):
    p=Ports(await flat())
    p.entry_report=replace(p.entry_report,post_attempted=False)
    monkeypatch.setattr(V40PortfolioSupervisor,"_permit_valid",staticmethod(lambda x:True))
    out=await controller(p).run_one(permit=object())
    assert out.status=="V40_SUPERVISOR_ENTRY_NOT_VERIFIED_RECONCILE"
    assert out.must_reconcile_before_next_cycle
    assert p.calls==["get","entry"]


@pytest.mark.asyncio
async def test_flat_guardian_scope_missing_halts_before_any_send():
    p=Ports(replace(await flat(),guardian=owned_guardian()))
    out=await controller(p).run_one()
    assert out.status=="V40_SUPERVISOR_GUARDIAN_ACCOUNT_OR_SOURCE_HALT"
    assert p.calls==["get"]


@pytest.mark.asyncio
async def test_active_critical_guardian_halts_first():
    f=active(1)
    p=Ports(replace(f,guardian=replace(f.guardian,
            no_current_scope_critical_or_pending_incidents=False)))
    out=await controller(p).run_one()
    assert out.status=="V40_SUPERVISOR_GUARDIAN_ACCOUNT_OR_SOURCE_HALT"
    assert p.calls==["get"]


@pytest.mark.asyncio
async def test_changed_recovered_ids_cannot_authorize_portfolio():
    f=active(3)
    p=Ports(replace(f,no_bar_due_ids=("99",),completed_exit=None))
    out=await controller(p).run_one()
    assert out.status=="V40_SUPERVISOR_GUARDIAN_ACCOUNT_OR_SOURCE_HALT"


@pytest.mark.asyncio
async def test_loop_halts_after_unverified_stop_no_retry(monkeypatch):
    p=Ports(active(1,low="86"))
    monkeypatch.setattr(V40PortfolioSupervisor,"_permit_valid",staticmethod(lambda x:True))
    result=await run_v40_portfolio_supervisor(
        controller(p),permit=object(),max_cycles=10,
        pause=lambda _:asyncio.sleep(0),
    )
    assert result.status=="V40_SUPERVISOR_STOP_GET_AND_CHECKPOINT_REQUIRED"
    assert p.calls==["get","stop"]


@pytest.mark.asyncio
async def test_loop_monitors_no_signal_with_fresh_get_each_time():
    p=Ports(await flat(False))
    result=await run_v40_portfolio_supervisor(
        controller(p),max_cycles=3,pause=lambda _:asyncio.sleep(0),
    )
    assert result.status=="V40_SUPERVISOR_WAIT_GENUINE_STAGE2"
    assert p.calls==["get"]*3
