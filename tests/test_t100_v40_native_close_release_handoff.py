"""V40 one-position positive native-close handoff: zero exchange POST.

These tests use existing independently constructed close/funding fixtures.
No network, DB connection, MEXC accounts or live-trading authorization.
"""
from dataclasses import replace
from decimal import Decimal as D
import pytest

from app.t100_live_owned_emergency_guardian import OwnedShort
from app.t100_live_readonly_probe import ReadOnlyMexcProbe
from app.t100_v40_unreleased_verified_slot_release import (
    PostgresV40VerifiedSlotRelease,V40SlotReleaseResult,
)
from app import t100_v40_native_close_release_handoff as module
from tests.test_t100_v40_unreleased_verified_slot_release import positive

SCOPE="a"*64


class GetOnly:
    async def _private_get(self,*args,**kwargs):
        raise AssertionError("TEST_SHOULD_USE_POSITIVE_WITNESS_FIXTURE")
    async def _private_post(self,*args,**kwargs):
        raise AssertionError("NEVER_POST_FROM_HANDOFF")


def owned(previous):
    return OwnedShort(
        scope_sha256=SCOPE,symbol=previous.symbol,
        position_id=previous.position_id,
        entry_external_oid=previous.external_oid,
        entry_order_id=previous.exchange_order_id,
        contracts=previous.contracts,
        stop_order_id=previous.stop_order_id,
        stop_price=previous.stop_price,
        checkpoint_version=previous.checkpoint_version,
    )


class WriterCalls:
    def __init__(self, result=None, exception=None):
        self.calls=[]
        self.result=result
        self.exception=exception
    async def close_and_release_once(self, **kwargs):
        self.calls.append(kwargs)
        if self.exception is not None:
            raise self.exception
        return self.result


def setup(monkeypatch,witness=None,result=None,exception=None):
    data=positive()
    good=data["witness"]
    seen=[]
    async def observe(**kwargs):
        seen.append(kwargs)
        return good if witness is None else witness
    monkeypatch.setattr(
        module,"observe_one_native_exit_among_v40_survivors",observe,
    )
    writer=PostgresV40VerifiedSlotRelease.__new__(
        PostgresV40VerifiedSlotRelease,
    )
    tracker=WriterCalls(
        result=result or V40SlotReleaseResult(
            "V40_CLOSE_DB_RELEASE_COMMITTED_RESTART_GET_REQUIRED",
            position_id=data["previous"].position_id,
            durable_release_and_close_committed=True,
        ),
        exception=exception,
    )
    writer.close_and_release_once=tracker.close_and_release_once
    handoff=module.V40NativeCloseReleaseHandoff(
        probe=ReadOnlyMexcProbe(GetOnly()),writer=writer,
    )
    return data,seen,tracker,handoff


async def run(handoff,data,**changes):
    return await handoff.run_once(
        closing=changes.get("closing",owned(data["previous"])),
        previous=changes.get("previous",data["previous"]),
        survivors=changes.get("survivors",()),
        accounting=changes.get("accounting",data["accounting"]),
        lease=changes.get("lease",data["lease"]),
        account_scope_sha256=changes.get("scope",SCOPE),
    )


@pytest.mark.asyncio
async def test_complete_native_close_and_accounting_single_atomic_release(monkeypatch):
    data,seen,writer,handoff=setup(monkeypatch)
    report=await run(handoff,data)
    assert report.status=="V40_CLOSE_HANDOFF_ONE_SLOT_DURABLY_RELEASED_NEW_GET_REQUIRED"
    assert report.durable_slot_release_verified is True
    assert report.database_commit_may_have_occurred is True
    assert report.fresh_account_ledger_guardian_recheck_required is True
    assert report.next_entry_allowed is False
    assert report.live_authorization is False
    assert report.mexc_posts==0
    assert len(seen)==1 and len(writer.calls)==1
    assert writer.calls[0]["previous"]==data["previous"]
    assert writer.calls[0]["accounting"]==data["accounting"]
    assert seen[0]["surviving"]==()


@pytest.mark.asyncio
async def test_mismatched_durable_origin_halts_before_any_get_or_sql(monkeypatch):
    data,seen,writer,handoff=setup(monkeypatch)
    wrong=replace(owned(data["previous"]),entry_external_oid="NOT_OWNED")
    report=await run(handoff,data,closing=wrong)
    assert report.status=="V40_CLOSE_HANDOFF_OWNED_POSITION_MISMATCH"
    assert seen==[] and writer.calls==[]


@pytest.mark.asyncio
async def test_manual_or_wrong_account_scope_not_adopted(monkeypatch):
    data,seen,writer,handoff=setup(monkeypatch)
    report=await run(handoff,data,scope="b"*64)
    assert report.status=="V40_CLOSE_HANDOFF_OWNED_POSITION_MISMATCH"
    assert seen==[] and writer.calls==[]


@pytest.mark.asyncio
@pytest.mark.parametrize("change",[
    {"status":"V40_PORTFOLIO_CLOSE_INITIAL_SIGNED_GET_UNVERIFIED"},
    {"exact_closed_position_history_verified":False},
    {"portfolio_unchanged_across_bookends":False},
    {"exact_close_order_id":"201"},
    {"surviving_protected_slots":1},
])
async def test_no_release_without_native_terminal_and_survivor_proofs(monkeypatch,change):
    data=positive()
    w=replace(data["witness"],**change)
    data,seen,writer,handoff=setup(monkeypatch,witness=w)
    report=await run(handoff,data)
    assert report.durable_slot_release_verified is False
    assert len(seen)==1
    assert writer.calls==[]


@pytest.mark.asyncio
@pytest.mark.parametrize("field",[
    "fill_history_complete",
    "funding_history_complete",
    "close_provenance_verified",
])
async def test_no_release_without_verified_closed_trade_accounting(monkeypatch,field):
    data,seen,writer,handoff=setup(monkeypatch)
    bad=replace(data["accounting"],**{field:False})
    report=await run(handoff,data,accounting=bad)
    assert report.status=="V40_CLOSE_HANDOFF_ACCOUNTING_OR_FILL_PROVENANCE_INVALID"
    assert len(seen)==1
    assert writer.calls==[]


@pytest.mark.asyncio
async def test_ambiguous_db_commit_is_never_reported_as_verified(monkeypatch):
    data,seen,writer,handoff=setup(
        monkeypatch,exception=TimeoutError("DB COMMIT ACK LOST SECRET"),
    )
    report=await run(handoff,data)
    assert report.status=="V40_CLOSE_HANDOFF_SQL_OUTCOME_UNKNOWN_RECONCILE_ONLY"
    assert report.database_commit_may_have_occurred is True
    assert report.durable_slot_release_verified is False
    assert report.next_entry_allowed is False and report.live_authorization is False
    assert len(writer.calls)==1
    assert "SECRET" not in str(report)


@pytest.mark.asyncio
async def test_wrong_writer_ack_stays_reconciliation_only(monkeypatch):
    data=positive()
    invalid=V40SlotReleaseResult(
        "V40_CLOSE_DB_RELEASE_COMMITTED_RESTART_GET_REQUIRED",
        position_id="999",durable_release_and_close_committed=True,
    )
    data,seen,writer,handoff=setup(monkeypatch,result=invalid)
    report=await run(handoff,data)
    assert report.status=="V40_CLOSE_HANDOFF_RELEASE_ACK_UNVERIFIED_RECONCILE_ONLY"
    assert report.durable_slot_release_verified is False
    assert report.database_commit_may_have_occurred is True
    assert len(writer.calls)==1
