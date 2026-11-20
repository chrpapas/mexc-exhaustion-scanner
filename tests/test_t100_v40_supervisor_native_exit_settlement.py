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



class ForwardSettlingPorts(Ports):
    def __init__(self,evidence,*,bad_ack=False,raise_unknown=False):
        super().__init__(evidence)
        self.bad_ack=bad_ack
        self.raise_unknown=raise_unknown

    async def finalize_verified_forward_native_close(self,*,lease):
        from app.t100_v40_forward_close_finalizer import V40ForwardCloseResult
        self.calls.append("FENCED_FINANCE_AND_SINGLE_SLOT_RELEASE")
        if self.raise_unknown:
            raise TimeoutError("sql may have committed")
        return V40ForwardCloseResult(
            status=(
                "UNKNOWN"
                if self.bad_ack else
                "V40_FORWARD_NATIVE_CLOSE_DURABLY_ACCOUNTED_AND_SLOT_RELEASED_REOBSERVE"
            ),
            position_id=self.evidence.previous.position_id,
            entry_and_close_fills_persisted=True,
            funding_persisted=True,
            final_accounting_persisted=True,
            slot_release_committed=not self.bad_ack,
            database_commit_may_have_occurred=True,
        )


@pytest.mark.asyncio
@pytest.mark.parametrize("n",[1,2,3,4])
async def test_supervisor_positive_intrabar_native_exit_fenced_finance_release(n):
    from app.t100_live_leader_fencing import LeaderLease
    p=ForwardSettlingPorts(pending(n))
    leader=LeaderLease("t100-live-executor","fenced-forward",8,45)
    report=await controller(p).run_one(close_lease=leader)
    assert report.status=="V40_SUPERVISOR_NATIVE_EXIT_ACCOUNTED_SLOT_RELEASED_REOBSERVE"
    assert report.must_reconcile_before_next_cycle is True
    assert not report.entries_allowed and not report.live_authorization
    assert p.calls==["GET_ONLY_MISMATCH","FENCED_FINANCE_AND_SINGLE_SLOT_RELEASE"]


@pytest.mark.asyncio
@pytest.mark.parametrize("fault",["stale_lease","bad_ack","unknown_commit"])
async def test_close_settlement_unknown_never_opens_or_retries(fault):
    from app.t100_live_leader_fencing import LeaderLease
    p=ForwardSettlingPorts(
        pending(3),bad_ack=fault=="bad_ack",
        raise_unknown=fault=="unknown_commit",
    )
    leader=LeaderLease(
        "t100-live-executor","fenced-forward",
        0 if fault=="stale_lease" else 8,45,
    )
    report=await controller(p).run_one(close_lease=leader)
    assert report.status in (
        "V40_SUPERVISOR_NATIVE_EXIT_FENCED_SETTLEMENT_UNAVAILABLE",
        "V40_SUPERVISOR_NATIVE_EXIT_SETTLEMENT_ACK_UNVERIFIED",
        "V40_SUPERVISOR_NATIVE_EXIT_DB_OUTCOME_UNKNOWN_RECONCILE",
    )
    assert report.must_reconcile_before_next_cycle
    assert not report.entries_allowed
    assert p.calls.count("FENCED_FINANCE_AND_SINGLE_SLOT_RELEASE")<=1


@pytest.mark.asyncio
async def test_close_leader_does_not_enable_open_for_flat_account():
    from app.t100_live_leader_fencing import LeaderLease
    from tests.test_t100_v40_live_portfolio_supervisor import (
        clean_flat,flat_guardian,source,
    )
    # Even with a valid close-only lease no opening permit is minted.
    class Flat:
        async def observe(self):
            from app.t100_v40_live_portfolio_supervisor import V40PortfolioCycleFrame
            return V40PortfolioCycleFrame(
                snapshot=await clean_flat(),guardian=flat_guardian(),
                source=source(True),completed_exit=None,
            )
        async def enter_one(self,**kwargs):
            raise AssertionError("close-only lease is not an entry permit")
    lease=LeaderLease("t100-live-executor","fenced-forward",8,45)
    report=await controller(Flat()).run_one(close_lease=lease)
    assert report.status=="V40_SUPERVISOR_ENTRY_PERMIT_UNAVAILABLE"
    assert not report.live_authorization
