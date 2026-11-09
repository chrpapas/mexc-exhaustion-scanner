"""Read-only V40 post-commit 1–4-slot restart evidence, no live permit."""
from __future__ import annotations

import asyncio
from copy import deepcopy
from dataclasses import asdict,replace
from decimal import Decimal as D

import pytest

from app.t100_live_durable_recovery import DurableRecoveryReport
from app.t100_live_four_slot_snapshot_coordinator import (
    FourSlotSnapshotCoordinator,FourSlotSnapshotResult,
    LedgerSnapshot,_ledger_digest,
)
from app.t100_live_supervisor import ManagedShort
from app.t100_live_readonly_probe import ReadOnlyMexcProbe
from tests.test_t100_live_stop_update_pipeline import stop
from tests.test_t100_live_v40_native_stop_recovery import position
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


def ledger_snapshot(x=None):
    return LedgerSnapshot(
        checkpoints=[asdict(x or expected())],revisions=[],
        intents=[],stop_amendments=[],readonly_repeatable_read=True,
    )


def result(x=None, *, ledger=None,venue="b"*64):
    x=x or expected()
    recovery=DurableRecoveryReport(
        recovered=(recovered(x),),
        checkpoint_count=1,exchange_position_count=1,
        exchange_stop_count=1,
    )
    return FourSlotSnapshotResult(
        status="NONFLAT_PROVENANCE_MATCHED_NONAUTHORIZING",
        occupied_slots=1,recovery=recovery,
        ledger_hash=(
            _ledger_digest(ledger_snapshot(x)) if ledger is None else ledger
        ),
        critical_inventory_hash=venue,
    )


def inspect(results):
    # This audit is deliberately only a consumer of the preexisting
    # read-only 4-slot coordinator; mock no exchange or DB mutation method.
    calls=[]
    class Ledger:
        async def read_snapshot(self):
            calls.append("LEDGER_READBACK")
            return ledger_snapshot()
    snap=FourSlotSnapshotCoordinator(
        ledger=Ledger(),venue=object(),completeness=object(),
    )
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
    assert calls==[
        "READ_ONLY_SNAPSHOT","LEDGER_READBACK",
        "READ_ONLY_SNAPSHOT","LEDGER_READBACK",
    ]


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
        result(
            replace(expected(),stop_order_id="101"),
            ledger=_ledger_digest(ledger_snapshot(expected())),
        ),
    ])
    out=execute(auditor)
    assert out.status=="POST_COMMIT_RECOVERED_CHECKPOINT_DRIFT_FREEZE"
    assert out.exchange_posts==0
    assert calls==[
        "READ_ONLY_SNAPSHOT","LEDGER_READBACK",
        "READ_ONLY_SNAPSHOT","LEDGER_READBACK",
    ]


def test_identical_recovered_stop_but_stale_durable_checkpoint_version_freezes():
    # ManagedShort has no revision number. Exact ledger reread must still
    # reject revision=1 even when all stop and quantity values are identical.
    auditor,calls=inspect([result(),result()])
    x=replace(expected(),checkpoint_version=3)
    out=execute(auditor,x)
    assert out.status=="POST_COMMIT_EXPECTED_REVISION_NOT_DURABLE_FREEZE"
    assert out.db_writes==0
    assert out.exchange_posts==0


def test_ledger_checksum_changes_after_completed_full_account_snapshot_freezes():
    auditor,calls=inspect([result(ledger="f"*64)])
    out=execute(auditor)
    assert out.status=="POST_COMMIT_LEDGER_READBACK_CHANGED_FREEZE"
    assert out.db_writes==0


def test_direct_durable_ledger_readback_failure_is_redacted():
    auditor,calls=inspect([result()])
    async def failed():
        raise TimeoutError("SECRET_DATABASE_DSN")
    auditor.snapshot.ledger.read_snapshot=failed
    out=execute(auditor)
    assert out.status=="POST_COMMIT_LEDGER_READBACK_UNVERIFIED_FREEZE"
    assert "SECRET" not in out.status
    assert out.db_writes==0


def with_signed_native_market_stop(auditor):
    class PrivateGetOnly:
        def __init__(self):
            self.calls=[]
            self.positions=[position()]
            self.stops=[stop(stopLossPrice=D("86.5"))]
            self.counts={
                "sumCount":1,"stopOrderCount":1,
                "limitOrderCount":0,"planOrderCount":0,"trackOrderCount":0,
            }
            self.mode={"positionMode":1}
            self.drift_extra_stop_field=False
            self.stop_reads=0
        async def _private_get(self,path,params=None):
            assert params is None
            self.calls.append(path)
            if path=="/api/v1/private/position/open_positions":
                return deepcopy(self.positions)
            if path=="/api/v1/private/stoporder/open_orders":
                self.stop_reads+=1
                out=deepcopy(self.stops)
                if self.drift_extra_stop_field:
                    out[0]["previously_unmodelled_exchange_field"]=self.stop_reads
                return out
            if path=="/api/v1/private/order/open_order_total_count":
                return deepcopy(self.counts)
            if path=="/api/v1/private/position/position_mode":
                return deepcopy(self.mode)
            raise AssertionError("UNAPPROVED_GET_ROUTE")
    client=PrivateGetOnly()
    auditor.probe=ReadOnlyMexcProbe(client)
    return client


def test_native_market_stop_valid_for_exact_cross_one_x_after_restart_is_nonauthorizing():
    auditor,calls=inspect([result(),result()])
    client=with_signed_native_market_stop(auditor)
    out=execute(auditor)
    assert out.status=="POST_COMMIT_NATIVE_MARKET_SL_GETS_STABLE_READONLY_NONAUTHORIZING"
    assert out.native_market_stop_type_verified_after_restart
    assert out.expected_checkpoint_recovered
    assert out.two_independent_full_account_snapshots_matched
    assert out.independent_process_restart_verified is False
    assert out.signed_get_transport_provenance_verified is False
    assert out.exchange_stop_atomicity_verified is False
    assert out.entry_allowed is False
    assert out.stop_mutation_allowed is False
    assert out.live_authorization is False
    assert out.exchange_posts==0
    assert out.db_writes==0
    assert len(client.calls)==8
    assert all(path.startswith("/api/v1/private/") for path in client.calls)


@pytest.mark.parametrize("fault",[
    "missing_stop_type","limit_stop","wrong_stop_quantity","wrong_stop_id",
    "wrong_margin_mode","wrong_leverage","wrong_position_qty",
    "hedge_disabled","open_limit_order","duplicate_stop","foreign_position",
    "stop_error","wrong_stop_reverse","stop_wrong_origin",
])
def test_native_stop_cross_restart_mismatch_fails_closed(fault):
    auditor,calls=inspect([result(),result()])
    client=with_signed_native_market_stop(auditor)
    if fault=="missing_stop_type":
        client.stops[0].pop("stopLossType")
    elif fault=="limit_stop":
        client.stops[0]["stopLossType"]=1
    elif fault=="wrong_stop_quantity":
        client.stops[0]["vol"]="19"
    elif fault=="wrong_stop_id":
        client.stops[0]["id"]="100"
    elif fault=="wrong_margin_mode":
        client.positions[0]["openType"]=1
    elif fault=="wrong_leverage":
        client.positions[0]["leverage"]=2
    elif fault=="wrong_position_qty":
        client.positions[0]["holdVol"]="19"
    elif fault=="hedge_disabled":
        client.mode={"positionMode":2}
    elif fault=="open_limit_order":
        client.counts={"sumCount":2,"stopOrderCount":1,
            "limitOrderCount":1,"planOrderCount":0,"trackOrderCount":0}
    elif fault=="duplicate_stop":
        client.stops.append(deepcopy(client.stops[0]))
    elif fault=="foreign_position":
        client.positions.append(dict(position(),positionId="11"))
    elif fault=="stop_error":
        client.stops[0]["errorCode"]=5
    elif fault=="wrong_stop_reverse":
        client.stops[0]["stopLossReverse"]=1
    elif fault=="stop_wrong_origin":
        client.stops[0]["orderId"]=123
    out=execute(auditor)
    assert out.status=="POST_COMMIT_SIGNED_NATIVE_STOP_GET_UNVERIFIED_FREEZE"
    assert out.native_market_stop_type_verified_after_restart is False
    assert not out.live_authorization
    assert out.exchange_posts==0
    assert out.db_writes==0


def test_new_exchange_stop_field_drifts_even_if_all_known_protection_fields_stable():
    auditor,calls=inspect([result(),result()])
    client=with_signed_native_market_stop(auditor)
    client.drift_extra_stop_field=True
    out=execute(auditor)
    assert out.status=="POST_COMMIT_NATIVE_STOP_GET_BOOKENDS_DRIFT_FREEZE"
    assert not out.live_authorization
    assert out.db_writes==0


def test_signed_get_timeout_keeps_readonly_restart_uncertified():
    auditor,calls=inspect([result(),result()])
    client=with_signed_native_market_stop(auditor)
    async def outage(path,params=None):
        raise TimeoutError("MEXC_PRIVATE_SECRET_NOT_REPORTABLE")
    client._private_get=outage
    out=execute(auditor)
    assert out.status=="POST_COMMIT_SIGNED_NATIVE_STOP_GET_UNVERIFIED_FREEZE"
    assert "SECRET" not in out.status
    assert out.exchange_posts==0
