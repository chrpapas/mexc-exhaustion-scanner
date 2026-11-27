"""V40 flat-after-close restart: every archived trade needs final accounting."""
import asyncio
from copy import deepcopy
from dataclasses import replace
from decimal import Decimal as D

import pytest

from app.t100_v40_unreleased_closed_history_audit import (
    V40ArchivedHistoryAudit,verify_closed_archive_rows,
)
from app.t100_live_four_slot_snapshot_coordinator import FourSlotSnapshotCoordinator
from tests.test_t100_live_four_slot_snapshot_coordinator import (
    fixture,report,Ledger,Venue,Completeness,
)


def evidence():
    intent=dict(
        external_oid="t100v1e001",symbol="ALPHA_USDT",
        exchange_order_id="100",state="CLOSED",
    )
    cp=dict(
        intent_oid=intent["external_oid"],intent_symbol=intent["symbol"],
        intent_entry_id="100",intent_state="CLOSED",
        position_id="10",checkpoint_oid=intent["external_oid"],
        checkpoint_symbol=intent["symbol"],checkpoint_entry_id="100",
        checkpoint_stop_id="99",checkpoint_contracts=D("2"),
        checkpoint_state="CLOSED",checkpoint_reason="CLOSE_VERIFIED",
        checkpoint_version=3,
        reservation_oid=intent["external_oid"],
        reservation_symbol=intent["symbol"],reservation_state="RELEASED",
        accounting_position_id="10",accounting_entry_id="100",
        accounting_close_id="200",accounting_stop_id="99",
        accounting_opened=D("2"),accounting_closed=D("2"),
        accounting_net=D("20.8"),
        revision_total=3,last_revision_version=3,
        last_revision_reason="CLOSE_VERIFIED",
        previous_revision_reason="CLOSING_LATCHED",
    )
    counts=dict(
        intents_total=1,intents_closed=1,
        checkpoints_total=1,checkpoints_closed=1,
        reservations_total=1,reservations_released=1,
        final_total=1,unresolved_stops=0,
    )
    return [intent],counts,[cp]


def audit(inputs=None):
    expected,counts,rows=inputs if inputs is not None else evidence()
    return verify_closed_archive_rows(
        expected_intents=expected,counts=counts,rows=rows,
    )


def test_positive_final_accounted_close_does_not_authorize_order():
    out=audit()
    assert out.status=="V40_ALL_ARCHIVED_NATIVE_CLOSES_ACCOUNTED_NONAUTHORIZING"
    assert out.verified_closed_positions==1
    assert out.every_archived_intent_accounted
    assert out.every_immutable_close_and_release_verified
    assert out.journal_unresolved_absent
    assert not out.account_flat_signed_get_verified
    assert not out.new_entry_allowed and not out.live_authorization
    assert out.database_writes==out.mexc_posts==0


@pytest.mark.parametrize("kind",[
    "missing_final_account","unreleased_reservation","partial_close",
    "wrong_stop","wrong_close_order","missing_revision",
    "wrong_last_reason","wrong_previous_reason",
    "extra_intent","unresolved_stop","unknown_intent",
    "unclosed_checkpoint","different_account","duplicated_position",
])
def test_any_historical_inconsistency_blocks_flat_restart(kind):
    expected,counts,rows=evidence()
    if kind=="missing_final_account":
        rows[0]["accounting_position_id"]=None
    if kind=="unreleased_reservation":
        rows[0]["reservation_state"]="PROTECTED"
    if kind=="partial_close":
        rows[0]["accounting_closed"]=D("1")
    if kind=="wrong_stop":
        rows[0]["accounting_stop_id"]="99x"
    if kind=="wrong_close_order":
        rows[0]["accounting_close_id"]=""
    if kind=="missing_revision":
        rows[0]["revision_total"]=2
    if kind=="wrong_last_reason":
        rows[0]["last_revision_reason"]="STOP_VERIFIED"
    if kind=="wrong_previous_reason":
        rows[0]["previous_revision_reason"]="STOP_VERIFIED"
    if kind=="extra_intent":
        counts["intents_total"]=2
    if kind=="unresolved_stop":
        counts["unresolved_stops"]=1
    if kind=="unknown_intent":
        rows[0]["intent_oid"]="unknown_oid"
    if kind=="unclosed_checkpoint":
        rows[0]["checkpoint_state"]="OPEN"
    if kind=="different_account":
        rows[0]["checkpoint_symbol"]="OTHER_USDT"
    if kind=="duplicated_position":
        row=deepcopy(rows[0])
        rows.append(row)
        counts.update(intents_total=2,intents_closed=2,
                      checkpoints_total=2,checkpoints_closed=2,
                      reservations_total=2,reservations_released=2,final_total=2)
    out=audit((expected,counts,rows))
    assert out.status!="V40_ALL_ARCHIVED_NATIVE_CLOSES_ACCOUNTED_NONAUTHORIZING"
    assert not out.every_immutable_close_and_release_verified
    assert not out.new_entry_allowed


@pytest.mark.asyncio
async def test_post_close_flat_account_uses_exact_archive_audit():
    ledger,orders,positions,stops=fixture(0)
    expected,counts,rows=evidence()
    ledger=replace(ledger,intents=deepcopy(expected))
    reader=Ledger(ledger)
    called=[]
    async def audit_archive(*,expected_intents):
        called.append(expected_intents)
        return audit((expected_intents,counts,rows))
    reader.audit_archived_close_history=audit_archive
    coordinator=FourSlotSnapshotCoordinator(
        ledger=reader,
        venue=Venue(orders,positions,stops),
        completeness=Completeness(report(0)),
    )
    result=await coordinator.inspect()
    assert result.status=="FLAT_OBSERVED_NONAUTHORIZING"
    assert result.entry_allowed is False and result.live_authorization is False
    assert len(called)==1
    assert called[0][0]["external_oid"]==expected[0]["external_oid"]


@pytest.mark.asyncio
async def test_dirty_historical_account_without_archive_proof_still_halts():
    ledger,orders,positions,stops=fixture(0)
    ledger=replace(ledger,intents=evidence()[0])
    result=await FourSlotSnapshotCoordinator(
        ledger=Ledger(ledger),
        venue=Venue(orders,positions,stops),
        completeness=Completeness(report(0)),
    ).inspect()
    assert result.status=="FLAT_HISTORY_REQUIRES_CLOSED_LEDGER_AUDIT"


@pytest.mark.asyncio
async def test_flat_after_close_never_accepts_fake_positive_archive():
    ledger,orders,positions,stops=fixture(0)
    ledger=replace(ledger,intents=evidence()[0])
    reader=Ledger(ledger)
    async def liar(*,expected_intents):
        return V40ArchivedHistoryAudit(
            "V40_ALL_ARCHIVED_NATIVE_CLOSES_ACCOUNTED_NONAUTHORIZING",
            verified_closed_positions=1,
            every_archived_intent_accounted=True,
            every_immutable_close_and_release_verified=True,
            journal_unresolved_absent=True,
            new_entry_allowed=True,
        )
    reader.audit_archived_close_history=liar
    result=await FourSlotSnapshotCoordinator(
        ledger=reader,
        venue=Venue(orders,positions,stops),
        completeness=Completeness(report(0)),
    ).inspect()
    assert result.status=="FLAT_HISTORY_ARCHIVED_CLOSE_FINALITY_UNVERIFIED"
