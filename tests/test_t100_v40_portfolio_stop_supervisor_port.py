"""Four-slot supervisor stop port -> frozen bar -> journal -> one POST.

Entirely offline; patch production release only within synthetic test.
"""
from __future__ import annotations
from dataclasses import replace
from decimal import Decimal as D
from unittest.mock import patch
import pytest

from app.t100_live_stop_amendment_coordinator import StopAmendmentLifecycleCoordinator
from app.t100_v40_unreleased_native_stop_cycle import V40UnreleasedStopCycleResult
from app.t100_v40_unreleased_exit_policy import V40ExitCheckpoint,V40ExitDecision
from app.t100_v40_unreleased_portfolio_exit_batch import (
    V40SlotExitReview,V40PortfolioExitBatch,
)
from app.t100_v40_unreleased_verified_exit_handoff import V40VerifiedExitProjection
from app.t100_v40_portfolio_stop_supervisor_port import (
    V40FourSlotStopLifecyclePort,V40LiveStopReadback,V40FourSlotStopPortHalted,
)
from app import t100_v40_portfolio_native_stop_transport as adapter
from app import t100_v40_portfolio_stop_supervisor_port as port
from tests.test_t100_v40_portfolio_native_stop_transport import (
    ready,permit,checkpoints,
)
from tests.test_t100_v40_native_stop_amend_transport import (
    preflight,MemoryJournal,
)


def projection(n=4):
    new=V40ExitCheckpoint(
        position_id="52",symbol="SIREN_USDT",
        entry_price=D("100"),filled_at_ts=1800,
        native_stop_price=D("99"),
        best_profit_pct=D("14"),trail_armed=True,
        last_completed_open_ts=3600,
    )
    decision=V40ExitDecision(
        "NATIVE_STOP_TIGHTENING_PROPOSED_UNAUTHORIZED",
        proposed_checkpoint=new,proposed_native_stop_price=D("99"),
        requires_exchange_stop_amendment=True,
    )
    review=V40SlotExitReview(
        position_id="52",symbol="SIREN_USDT",decision=decision,
        requires_native_order_reconciliation=False,
        requires_native_stop_amendment=True,proposed_stop_price=D("99"),
    )
    batch=V40PortfolioExitBatch(
        "V40_PORTFOLIO_NATIVE_STOP_AMENDMENTS_UNVERIFIED",
        slots=(review,),native_close_reconciliation_count=0,
        proposed_stop_amendment_count=1,
        unchanged_verified_stop_count=0,
        any_unresolved_venue_transition=True,
    )
    return V40VerifiedExitProjection(
        status="V40_VERIFIED_OWNED_ONE_TO_FOUR_EXITS_PROJECTED_NONAUTHORIZING",
        protected_position_count=n,
        verified_exchange_and_ledger_bookends=True,
        verified_durable_bar_cursors=True,batch=batch,
        native_close_reconciliation_count=0,
        candidate_native_stop_amendment_count=1,
    )


class Preflight:
    def __init__(self,rows):
        self.rows=rows
        self.calls=[]
    async def observe(self, position_id):
        self.calls.append(position_id)
        return V40LiveStopReadback(
            checkpoints=self.rows,target_signal_id=497,
            preflight=preflight(),
        )


def assembly(n,monkeypatch):
    c,transport,guardian=ready(n,monkeypatch)
    journal=MemoryJournal()
    watcher=Preflight(checkpoints(n))
    lifecycle=StopAmendmentLifecycleCoordinator(journal)
    p=V40FourSlotStopLifecyclePort(
        client=c,ledger=transport.ledger,
        guardian_reader=guardian,stop_lifecycle=lifecycle,
        preflight=watcher,
    )
    return p,c,journal,watcher,guardian


@pytest.mark.asyncio
async def test_global_release_fails_before_preflight_journal_or_network(monkeypatch):
    p,c,j,r,g=assembly(4,monkeypatch)
    with pytest.raises(V40FourSlotStopPortHalted,match="CONTRACT_UNCERTIFIED"):
        await p.tighten_one_stop(projection(4),permit=permit())
    assert not r.calls and not j.rows and not c.posts and not c.gets


@pytest.mark.asyncio
@pytest.mark.parametrize("n",[1,2,3,4])
async def test_full_frozen_stop_projection_to_durable_single_submit(n,monkeypatch):
    p,c,j,r,g=assembly(n,monkeypatch)
    monkeypatch.setattr(port,"require_certified_transport_permit",lambda x:x)
    monkeypatch.setattr(
        "app.t100_live_stop_mutation_executor.require_certified_transport_permit",
        lambda x:x,
    )
    with patch("app.t100_live_guards.LIVE_NATIVE_STOP_AMENDMENT_STATUS","CERTIFIED"):
        result=await p.tighten_one_stop(projection(n),permit=permit())
        assert type(result) is V40UnreleasedStopCycleResult
        assert result.status=="V40_TRAIL_ONE_STOP_POST_ATTEMPT_RECONCILE_ONLY"
        assert result.stop_post_may_have_occurred is True
        assert result.requires_independent_reconciliation is True
        assert result.new_entries_allowed is False
        assert result.live_authorization is False
        assert result.orders_sent==0
        assert r.calls==["52"]
        assert j.rows[result.amendment.external_id]=="SUBMITTING"
        assert len(c.posts)==1
        second=await p.tighten_one_stop(projection(n),permit=permit())
        assert second.status=="V40_TRAIL_EXISTING_ATTEMPT_RECONCILE_ONLY"
        assert len(c.posts)==1


@pytest.mark.asyncio
async def test_unknown_exchange_post_persists_unknown_without_second_order(monkeypatch):
    p,c,j,r,g=assembly(4,monkeypatch)
    async def error(path,body):
        c.posts.append((path,body))
        raise TimeoutError("PRIVATE EXCHANGE KEY URL")
    c._private_post=error
    monkeypatch.setattr(port,"require_certified_transport_permit",lambda x:x)
    monkeypatch.setattr(
        "app.t100_live_stop_mutation_executor.require_certified_transport_permit",
        lambda x:x,
    )
    with patch("app.t100_live_guards.LIVE_NATIVE_STOP_AMENDMENT_STATUS","CERTIFIED"):
        first=await p.tighten_one_stop(projection(4),permit=permit())
        assert first.status=="V40_TRAIL_POST_UNKNOWN_RECONCILE_ONLY"
        assert j.rows[first.amendment.external_id]=="UNKNOWN"
        again=await p.tighten_one_stop(projection(4),permit=permit())
        assert again.status=="V40_TRAIL_EXISTING_ATTEMPT_RECONCILE_ONLY"
        assert len(c.posts)==1


@pytest.mark.asyncio
async def test_multiple_simultaneous_amendments_cannot_arbitrarily_select_first(monkeypatch):
    p,c,j,r,g=assembly(4,monkeypatch)
    x=projection(4)
    row=x.batch.slots[0]
    two=replace(row,position_id="53",symbol="ALT1_USDT",
                decision=replace(row.decision,
                    proposed_checkpoint=replace(row.decision.proposed_checkpoint,
                        position_id="53",symbol="ALT1_USDT")))
    new_batch=replace(x.batch,slots=(row,two),
                      proposed_stop_amendment_count=2)
    x=replace(x,batch=new_batch,
              candidate_native_stop_amendment_count=2)
    monkeypatch.setattr(port,"require_certified_transport_permit",lambda x:x)
    with patch("app.t100_live_guards.LIVE_NATIVE_STOP_AMENDMENT_STATUS","CERTIFIED"):
        with pytest.raises(
            V40FourSlotStopPortHalted,match="NOT_SINGLE_SAFE_ACTION",
        ):
            await p.tighten_one_stop(x,permit=permit())
    assert not r.calls and not j.rows and not c.posts


@pytest.mark.asyncio
async def test_cannot_reuse_canary_permit_or_mismatched_bar_preflight(monkeypatch):
    p,c,j,r,g=assembly(2,monkeypatch)
    monkeypatch.setattr(port,"require_certified_transport_permit",lambda x:x)
    with patch("app.t100_live_guards.LIVE_NATIVE_STOP_AMENDMENT_STATUS","CERTIFIED"):
        v=permit()
        v.scope="T100_V40_SUPERVISED_FIRST_SIGNAL"
        with pytest.raises(V40FourSlotStopPortHalted,match="CANARY_SCOPE_FORBIDDEN"):
            await p.tighten_one_stop(projection(2),permit=v)
        v=permit()
        r.observe=bad_observe
        with pytest.raises(
            V40FourSlotStopPortHalted,match="PREFLIGHT_DOES_NOT_MATCH_BAR",
        ):
            await p.tighten_one_stop(projection(2),permit=v)
    assert not j.rows and not c.posts


async def bad_observe(position_id):
    prior=preflight()
    return V40LiveStopReadback(
        checkpoints=checkpoints(2),target_signal_id=497,
        preflight=replace(prior,proposal=replace(
            prior.proposal,proposed_stop=D("97"),
        )),
    )
