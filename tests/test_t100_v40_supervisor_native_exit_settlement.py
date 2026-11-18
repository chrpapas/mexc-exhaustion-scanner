"""A genuine intrabar SL must enter funded settlement, NOT entry or stop POST."""
from dataclasses import replace

import pytest

from app.t100_live_durable_recovery import checkpoint_from_row
from app.t100_v40_native_close_mismatch_recovery import (
    V40NativeCloseMismatchEvidence,
)
from app.t100_v40_live_portfolio_supervisor import V40PortfolioSupervisor
from tests.test_t100_v40_native_close_mismatch_recovery import setup,positive_for
from tests.test_t100_v40_live_portfolio_supervisor import controller


def pending(n=3):
    s=setup(n)
    from app.t100_live_durable_recovery import checkpoint_from_row
    cp=s.cp
    return V40NativeCloseMismatchEvidence(
        status="V40_EXACT_ONE_NATIVE_EXIT_POSITIVE_ACCOUNTING_AND_RELEASE_PENDING",
        previous=cp,
        survivors=tuple(checkpoint_from_row(row) for row in s.original.checkpoints[1:]),
        witness=positive_for(cp,n),
    )


class Ports:
    def __init__(self,evidence):
        self.evidence=evidence
        self.calls=[]
    async def observe(self):
        self.calls.append("GET_ONLY_MISMATCH")
        return self.evidence
    async def enter_one(self,**kwargs):
        self.calls.append("EXCHANGE_ENTRY_POST")
        raise AssertionError("an exchange-close mismatch cannot allow new entry")
    async def tighten_one_stop(self,*args,**kwargs):
        self.calls.append("EXCHANGE_STOP_POST")
        raise AssertionError("cannot edit stops during closed-position mismatch")
    async def release_verified_native_close(self,*args,**kwargs):
        self.calls.append("UNFUNDED_DB_RELEASE")
        raise AssertionError("cannot recycle a slot before final funding")


@pytest.mark.asyncio
@pytest.mark.parametrize("n",[1,2,3,4])
async def test_positive_native_close_never_allows_entry_or_slot_release(n):
    evidence=pending(n)
    ports=Ports(evidence)
    report=await controller(ports).run_one(permit=object())
    assert report.status=="V40_SUPERVISOR_NATIVE_EXIT_POSITIVE_SETTLEMENT_REQUIRED"
    assert report.observed_slots==n
    assert report.must_reconcile_before_next_cycle is True
    assert report.entries_allowed is False
    assert report.live_authorization is False
    assert ports.calls==["GET_ONLY_MISMATCH"]


@pytest.mark.asyncio
@pytest.mark.parametrize("change",[
    {"funding_and_final_accounting_verified":True},
    {"slot_release_permitted":True},
    {"next_entry_allowed":True},
    {"positive_native_close_verified":False},
    {"venue_bookends_equal":False},
    {"ledger_bookends_equal":False},
    {"live_authorization":True},
    {"mexc_posts":1},
    {"database_writes":1},
    {"status":"FAKE_CLOSE"},
])
async def test_unsupported_certifications_from_read_only_close_fail_closed(change):
    ports=Ports(replace(pending(2),**change))
    report=await controller(ports).run_one(permit=object())
    assert report.status=="V40_SUPERVISOR_NATIVE_EXIT_MISMATCH_INVALID"
    assert report.must_reconcile_before_next_cycle
    assert not report.live_authorization
    assert ports.calls==["GET_ONLY_MISMATCH"]


@pytest.mark.asyncio
async def test_wrong_exact_closed_position_identity_never_releases():
    evidence=pending(3)
    ports=Ports(replace(
        evidence,witness=replace(evidence.witness,closed_position_id="9999"),
    ))
    report=await controller(ports).run_one(permit=object())
    assert report.status=="V40_SUPERVISOR_NATIVE_EXIT_MISMATCH_INVALID"
    assert ports.calls==["GET_ONLY_MISMATCH"]
