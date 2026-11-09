"""Read-only V40 post-commit 1–4-slot restart evidence, no live permit."""
from __future__ import annotations

import asyncio
from dataclasses import replace
from decimal import Decimal as D

import pytest

from app.t100_live_durable_recovery import DurableRecoveryReport
from app.t100_live_four_slot_snapshot_coordinator import (
    FourSlotSnapshotCoordinator,FourSlotSnapshotResult,
)
from app.t100_live_supervisor import ManagedShort
from app.t100_live_v40_post_commit_restart_audit import (
    V40PostCommitRestartAuditor,
)
from tests.test_t100_live_stop_update_pipeline import cp


def expected():
    return replace(
        cp(),stop_price=D("86.5"),best_profit_pct=D("14"),
        armed=True,checkpoint_version=2,
    )


def recovered(x):
    return ManagedShort(
        symbol=x.symbol,position_id=x.position_id,
        contracts=x.contracts,entry_price=x.entry_price,
        stop_order_id=x.stop_order_id,stop_price=x.stop_price,
        best_profit_pct=x.best_profit_pct,armed=x.armed,
    )


def result(x=None, *, ledger="a"*64,venue="b"*64):
    x=x or expected()
    recovery=DurableRecoveryReport(
        recovered=(recovered(x),),
        checkpoint_count=1,exchange_position_count=1,
        exchange_stop_count=1,
    )
    return FourSlotSnapshotResult(
        status="NONFLAT_PROVENANCE_MATCHED_NONAUTHORIZING",
        occupied_slots=1,recovery=recovery,
        ledger_hash=ledger,critical_inventory_hash=venue,
    )


def inspect(results):
    # This audit is deliberately only a consumer of the preexisting
    # read-only 4-slot coordinator; mock no exchange or DB mutation method.
    snap=FourSlotSnapshotCoordinator(
        ledger=object(),venue=object(),completeness=object(),
    )
    calls=[]
    it=iter(results)
    async def read(*,shared_manual_observation=False):
        assert shared_manual_observation is False
        calls.append("READ_ONLY_SNAPSHOT")
        item=next(it)
        if isinstance(item,Exception):raise item
        return item
    snap.inspect=read
    return V40PostCommitRestartAuditor(snap),calls


def execute(auditor,x=None):
    return asyncio.run(auditor.observe(expected=x or expected()))


def test_two_independent_full_account_restart_snapshots_stable_no_authorization():
    auditor,calls=inspect([result(),result()])
    out=execute(auditor)
    assert out.status=="POST_COMMIT_DURABLE_EXCHANGE_RECOVERED_READONLY_NONAUTHORIZING"
    assert out.recovered_slots==1
    assert out.expected_checkpoint_recovered
    assert out.two_independent_full_account_snapshots_matched
    assert out.independent_process_restart_verified is False
    assert out.native_market_stop_type_verified_after_restart is False
    assert out.exchange_stop_atomicity_verified is False
    assert out.signed_get_transport_provenance_verified is False
    assert not out.entry_allowed
    assert not out.stop_mutation_allowed
    assert not out.live_authorization
    assert out.exchange_posts==0
    assert out.db_writes==0
    assert calls==["READ_ONLY_SNAPSHOT"]*2


@pytest.mark.parametrize("fault",[
    "ledger_drift","venue_drift","checkpoint_stop_drift","missing_target",
    "incomplete_slot","bad_provenance","bad_hash","bad_recovery_shape",
    "unexpected_authorization","read_timeout",
])
def test_any_failed_post_commit_restart_proof_freezes(fault):
    a=result()
    b=result()
    if fault=="ledger_drift": b=replace(b,ledger_hash="c"*64)
    if fault=="venue_drift": b=replace(b,critical_inventory_hash="c"*64)
    if fault=="checkpoint_stop_drift":
        changed=replace(expected(),stop_price=D("80"))
        b=result(changed)
    if fault=="missing_target":
        changed=replace(expected(),position_id="11")
        b=result(changed)
    if fault=="incomplete_slot":
        b=replace(b,occupied_slots=2)
    if fault=="bad_provenance":
        b=replace(b,status="DURABLE_EXCHANGE_PROVENANCE_MISMATCH")
    if fault=="bad_hash":
        b=replace(b,ledger_hash="not-a-sha")
    if fault=="bad_recovery_shape":
        b=replace(
            b,recovery=DurableRecoveryReport(
                recovered=(recovered(expected()),),
                checkpoint_count=2,exchange_position_count=2,
                exchange_stop_count=2,
            ),
        )
    if fault=="unexpected_authorization":
        b=replace(b,live_authorization=True)
    if fault=="read_timeout": b=TimeoutError("SECRET_PRIVATE_ERROR")
    auditor,calls=inspect([a,b])
    out=execute(auditor)
    assert out.status.endswith("_FREEZE")
    assert not out.expected_checkpoint_recovered
    assert not out.two_independent_full_account_snapshots_matched
    assert not out.live_authorization
    assert not out.entry_allowed
    assert out.exchange_posts==0
    assert out.db_writes==0
    assert "SECRET_PRIVATE_ERROR" not in out.status


@pytest.mark.parametrize("field,value",[
    ("checkpoint_version",1),("lifecycle_state","REVIEW"),
    ("stop_order_id","not-valid"),("armed",False),
    ("best_profit_pct",D("12")),
])
def test_invalid_expected_restarted_checkpoint_does_not_read_database(field,value):
    x=replace(expected(),**{field:value})
    auditor,calls=inspect([result(),result()])
    out=execute(auditor,x)
    assert out.status=="POST_COMMIT_EXPECTED_CHECKPOINT_INVALID_FREEZE"
    assert calls==[]


def test_second_recovery_must_not_be_a_different_position_or_stop():
    auditor,calls=inspect([
        result(),
        result(replace(expected(),stop_order_id="101")),
    ])
    out=execute(auditor)
    assert out.status=="POST_COMMIT_RECOVERED_CHECKPOINT_DRIFT_FREEZE"
    assert out.exchange_posts==0
    assert len(calls)==2
