"""Require full V40 0–3-slot signal identity before ANY durable write.

The source is synthetic and the permit is locally mocked ONLY so the
new early monitors can be tested. Production release stays hard closed.
"""
from __future__ import annotations

from dataclasses import replace
from decimal import Decimal as D
from types import SimpleNamespace
from unittest.mock import patch
import pytest

from app.t100_live_execution import LiveCaps
from app.t100_live_v40_entry_coordinator import (
    V40OneShotCoordinator,V40CoordinatorHalted,
)
from tests.test_t100_v40_live_portfolio_supervisor import source


class TapeReader:
    def __init__(self,batch):
        self.batch=batch
        self.calls=0
    async def latest_candidates(self,*,observed_at):
        self.calls+=1
        return self.batch


class NoClaims:
    def __init__(self):
        self.calls=0
    async def acquire(self,holder_id):
        self.calls+=1
        raise RuntimeError("STOP_BEFORE_LEASE_TEST")


def runner(batch):
    obj=V40OneShotCoordinator.__new__(V40OneShotCoordinator)
    obj.source=TapeReader(batch)
    obj.initial_pilot_only=False
    obj.leaders=NoClaims()
    return obj


def permit(batch):
    row=batch.candidates[0]
    return SimpleNamespace(
        scope="T100_V40_FOUR_SLOT_PRODUCTION_TRIAL",
        signal_id=row.signal_id,symbol=row.symbol,
    )


def options(row):
    return dict(
        permit=row,
        caps=LiveCaps(
            D("100"),D("300"),D("50"),leverage=3,
            risk_profile="T100_V40_FOUR_SLOT_PRODUCTION_TRIAL",
        ),
        parity_report={},holder_id="v40-coordinator-test",
        monitored_signal_id=row.signal_id,
        monitored_symbol=row.symbol,
        monitored_tier="HIGH_225",
    )


@pytest.mark.asyncio
async def test_portfolio_scope_unmonitored_never_reads_signal_or_claims():
    tape=source(True)
    c=runner(tape)
    p=permit(tape)
    args=options(p)
    args.pop("monitored_signal_id")
    with patch(
        "app.t100_live_v40_entry_coordinator.require_certified_transport_permit",
        lambda x:x,
    ):
        with pytest.raises(V40CoordinatorHalted,match="EXACT_MONITORED_STAGE2"):
            await c.run_one(**args)
    assert c.source.calls==0
    assert c.leaders.calls==0


@pytest.mark.asyncio
async def test_portfolio_scope_wrong_signal_never_reads_or_claims():
    tape=source(True)
    c=runner(tape)
    p=permit(tape)
    args=options(p)
    args["monitored_signal_id"]=p.signal_id+10
    with patch(
        "app.t100_live_v40_entry_coordinator.require_certified_transport_permit",
        lambda x:x,
    ):
        with pytest.raises(V40CoordinatorHalted,match="EXACT_MONITORED_STAGE2"):
            await c.run_one(**args)
    assert c.source.calls==0 and c.leaders.calls==0


@pytest.mark.asyncio
async def test_portfolio_scope_same_bar_competition_never_claims(monkeypatch):
    tape=source(True)
    row=tape.candidates[0]
    second=replace(row,signal_id=row.signal_id+1)
    c=runner(replace(tape,candidates=(row,second)))
    p=permit(tape)
    args=options(p)
    args["monitored_tier"]=row.tier
    monkeypatch.setattr(
        "app.t100_live_v40_entry_coordinator.assess_strategy_parity_report",
        lambda _:{"strategy_parity_verified":True},
    )
    with patch(
        "app.t100_live_v40_entry_coordinator.require_certified_transport_permit",
        lambda x:x,
    ):
        with pytest.raises(V40CoordinatorHalted,match="MONITORED_SIGNAL_CHANGED_OR_NOT_UNIQUE"):
            await c.run_one(**args)
    assert c.source.calls==1 and c.leaders.calls==0


@pytest.mark.asyncio
async def test_portfolio_scope_exact_single_candidate_can_reach_fenced_acquire(monkeypatch):
    tape=source(True)
    row=tape.candidates[0]
    c=runner(tape)
    p=permit(tape)
    args=options(p)
    args["monitored_tier"]=row.tier
    monkeypatch.setattr(
        "app.t100_live_v40_entry_coordinator.assess_strategy_parity_report",
        lambda _:{"strategy_parity_verified":True},
    )
    with patch(
        "app.t100_live_v40_entry_coordinator.require_certified_transport_permit",
        lambda x:x,
    ):
        with pytest.raises(RuntimeError,match="STOP_BEFORE_LEASE_TEST"):
            await c.run_one(**args)
    assert c.source.calls==1 and c.leaders.calls==1


@pytest.mark.asyncio
async def test_untouched_canary_and_legacy_scope_are_not_promoted():
    tape=source(True)
    c=runner(tape)
    p=SimpleNamespace(scope="T100_V40_SUPERVISED_FIRST_SIGNAL")
    args=options(permit(tape))
    args["permit"]=p
    with patch(
        "app.t100_live_v40_entry_coordinator.require_certified_transport_permit",
        lambda x:x,
    ):
        with pytest.raises(V40CoordinatorHalted,match="CANARY_PINNED_REAL_SIGNAL"):
            await c.run_one(**args)
    assert c.source.calls==0 and c.leaders.calls==0
