"""Supervisor must re-observe after proven native close + SQL slot release.

Synthetic typed close/fill witnesses; no MEXC network or SQL writes.
A test-only simulated permit is injected for positive release assertions;
production permit issuance and transport certification remain untouched.
"""
from dataclasses import replace
from decimal import Decimal as D
import pytest

from app.t100_v40_live_portfolio_supervisor import V40PortfolioSupervisor
from app.t100_v40_native_close_release_handoff import (
    V40NativeCloseReleaseHandoffReport,
)
from tests.test_t100_v40_live_portfolio_supervisor import (
    Ports,active,controller,
)
from tests.test_t100_v40_unreleased_verified_slot_release import positive


class PositivePorts(Ports):
    def __init__(self,frame):
        super().__init__(frame)
        self.bad_identity=False
        self.bad_volume=False
        self.fault=None
        self.bad_ack=False

    async def reconcile_native_close(self,projection):
        self.calls.append("close")
        due=tuple(
            x for x in projection.batch.slots
            if x.requires_native_order_reconciliation
        )
        active_owned=self.frame.snapshot.recovery.recovered
        target=due[0]
        cp=next(p for p in active_owned if p.position_id==target.position_id)
        template=positive()["witness"]
        fill=replace(
            template.closing_fills,
            position_id=target.position_id,
            symbol=target.symbol,
            deal_volume=(cp.contracts+D("1")
                         if self.bad_volume else cp.contracts),
        )
        return replace(
            template,
            closed_position_id="999" if self.bad_identity else target.position_id,
            closing_fills=fill,
            surviving_protected_slots=self.frame.snapshot.occupied_slots-1,
        )

    async def release_verified_native_close(self,projection,witness,*,permit):
        assert permit is not None
        self.calls.append("release")
        if self.fault is not None:
            raise self.fault
        if self.bad_ack:
            return V40NativeCloseReleaseHandoffReport(
                "V40_CLOSE_HANDOFF_SQL_OUTCOME_UNKNOWN_RECONCILE_ONLY",
                database_commit_may_have_occurred=True,
            )
        return V40NativeCloseReleaseHandoffReport(
            "V40_CLOSE_HANDOFF_ONE_SLOT_DURABLY_RELEASED_NEW_GET_REQUIRED",
            closed_position_id=witness.closed_position_id,
            exact_exchange_close_verified=True,
            persisted_accounting_verified=True,
            durable_slot_release_verified=True,
            database_commit_may_have_occurred=True,
        )


@pytest.fixture
def simulated_portfolio_permit(monkeypatch):
    """Authorize only an exact test sentinel; never alter production gates."""
    approved=object()
    monkeypatch.setattr(
        V40PortfolioSupervisor,"_permit_valid",
        staticmethod(lambda permit:permit is approved),
    )
    return approved


@pytest.mark.asyncio
async def test_verified_one_native_close_without_permit_never_releases():
    p=PositivePorts(active(4,due_indices=(0,),high="180",low="80"))
    out=await controller(p).run_one()
    assert out.status=="V40_SUPERVISOR_CLOSE_RELEASE_PERMIT_UNAVAILABLE"
    assert out.must_reconcile_before_next_cycle is True
    assert out.live_authorization is False and out.entries_allowed is False
    assert p.calls==["get","close"]


@pytest.mark.asyncio
async def test_verified_one_native_close_db_release_never_enters_same_cycle(
    simulated_portfolio_permit,
):
    p=PositivePorts(active(4,due_indices=(0,),high="180",low="80"))
    out=await controller(p).run_one(permit=simulated_portfolio_permit)
    assert out.status=="V40_SUPERVISOR_ONE_CLOSED_SLOT_RELEASED_NEW_GET_REQUIRED"
    assert out.must_reconcile_before_next_cycle is True
    assert out.origin_may_have_mutated is False
    assert out.entries_allowed is False and out.live_authorization is False
    assert p.calls==["get","close","release"]


@pytest.mark.asyncio
@pytest.mark.parametrize("attribute",["bad_identity","bad_volume"])
async def test_inaccurate_native_close_provenance_blocks_db_release(attribute):
    p=PositivePorts(active(2,due_indices=(0,),high="180",low="80"))
    setattr(p,attribute,True)
    out=await controller(p).run_one()
    assert out.status=="V40_SUPERVISOR_NATIVE_EXIT_RECONCILE_DURABLE_RELEASE_REQUIRED"
    assert p.calls==["get","close"]
    assert out.entries_allowed is False


@pytest.mark.asyncio
async def test_ambiguous_sql_release_is_terminal_no_retry(simulated_portfolio_permit):
    p=PositivePorts(active(3,due_indices=(0,),high="180",low="80"))
    p.fault=TimeoutError("SECRET DB COMMIT STATUS UNKNOWN")
    out=await controller(p).run_one(permit=simulated_portfolio_permit)
    assert out.status=="V40_SUPERVISOR_NATIVE_CLOSE_DB_OUTCOME_UNKNOWN_RECONCILE"
    assert out.must_reconcile_before_next_cycle is True
    assert out.live_authorization is False
    assert p.calls==["get","close","release"]
    assert "SECRET" not in out.status


@pytest.mark.asyncio
async def test_bad_release_ack_stays_reconciliation_only(simulated_portfolio_permit):
    p=PositivePorts(active(3,due_indices=(0,),high="180",low="80"))
    p.bad_ack=True
    out=await controller(p).run_one(permit=simulated_portfolio_permit)
    assert out.status=="V40_SUPERVISOR_NATIVE_CLOSE_RELEASE_UNVERIFIED_RECONCILE"
    assert out.must_reconcile_before_next_cycle is True
    assert p.calls==["get","close","release"]


@pytest.mark.asyncio
async def test_two_suspected_simultaneous_native_closes_never_release_only_one():
    p=PositivePorts(active(4,due_indices=(0,1),high="180",low="80"))
    # The shared fixture touches only the first due symbol's stop. Model
    # a second unresolved native close in the same batch explicitly.
    projection=p.frame.completed_exit
    two_closes=replace(
        projection.batch,native_close_reconciliation_count=2,
    )
    p.frame=replace(
        p.frame,
        completed_exit=replace(
            projection,batch=two_closes,
            native_close_reconciliation_count=2,
        ),
    )
    out=await controller(p).run_one()
    assert out.status=="V40_SUPERVISOR_NATIVE_EXIT_RECONCILE_DURABLE_RELEASE_REQUIRED"
    assert p.calls==["get","close"]
    assert out.entries_allowed is False


@pytest.mark.asyncio
async def test_run_loop_does_not_repeat_after_positive_release(simulated_portfolio_permit):
    from app.t100_v40_live_portfolio_supervisor import run_v40_portfolio_supervisor
    p=PositivePorts(active(2,due_indices=(0,),high="180",low="80"))
    out=await run_v40_portfolio_supervisor(
        controller(p),permit=simulated_portfolio_permit,
        max_cycles=20,interval_seconds=3,
    )
    assert out.status=="V40_SUPERVISOR_ONE_CLOSED_SLOT_RELEASED_NEW_GET_REQUIRED"
    assert p.calls==["get","close","release"]
