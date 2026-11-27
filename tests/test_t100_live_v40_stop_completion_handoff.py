"""V40 stop completion handoff: no POST, permit gates, durable CAS evidence."""
from __future__ import annotations

import asyncio
from copy import deepcopy
from dataclasses import asdict
from decimal import Decimal as D

import pytest

import app.t100_live_v40_stop_completion_handoff as m
from app.t100_live_authorization import LiveAuthorizationHalted
from app.t100_live_checkpoint_coordinator import CheckpointPersistenceCoordinator
from app.t100_live_readonly_probe import ReadOnlyMexcProbe
from app.t100_live_stop_amendment_coordinator import StopAmendmentLifecycleCoordinator
from app.t100_live_stop_journal import StopAmendment
from app.t100_live_stop_update_pipeline import StopUpdatePersistencePipeline
from app.t100_live_v40_native_stop_recovery import V40NativeStopRecoveryAuditor
from app.t100_live_v40_trailing_restart import reconstruct_v40_trailing_after_restart
from tests.test_t100_live_stop_update_pipeline import (
    Journal,Writer,cp,stop,
)
from tests.test_t100_live_v40_native_stop_recovery import (
    Ledger,Venue,Completeness,position,
)


def fixture(*,price="86.5",old_stop=False,replacement=False):
    a=StopAmendment.build(
        symbol="SIREN_USDT",position_id="10",
        stop_order_id="99",previous_stop=D("175"),
        proposed_stop=D("86.5"),
    )
    journal=Journal()
    journal.states[a.external_id]="UNKNOWN"
    ledger=Ledger(a,state="UNKNOWN")
    venue=Venue(
        price=D("175") if old_stop else D(price),
        stop_id="100" if replacement else "99",
    )
    auditor=V40NativeStopRecoveryAuditor(
        ledger=ledger,venue=venue,
        completeness=Completeness(),
        journal=journal,
    )
    writer=Writer()
    pipeline=StopUpdatePersistencePipeline(
        StopAmendmentLifecycleCoordinator(journal),
        CheckpointPersistenceCoordinator(writer),
    )

    class FakePrivateGet:
        def __init__(self):
            self.calls=[]
            self.stop_rows=[stop(
                stopLossPrice=D("175") if old_stop else D(price),
                id="100" if replacement else "99",
            )]
            self.pos_rows=[position()]
            self.bad=False
        async def _private_get(self,path,params=None):
            self.calls.append(path)
            if self.bad:
                raise TimeoutError("SECRET_REQUEST_TOKEN")
            values={
                "/api/v1/private/position/open_positions":self.pos_rows,
                "/api/v1/private/stoporder/open_orders":self.stop_rows,
                "/api/v1/private/order/open_order_total_count":{
                    "sumCount":1,"stopOrderCount":1,
                    "limitOrderCount":0,"planOrderCount":0,
                    "trackOrderCount":0,
                },
                "/api/v1/private/position/position_mode":{
                    "positionMode":1,
                },
            }
            return deepcopy(values[path])

    private=FakePrivateGet()
    handoff=m.V40NativeStopCompletionHandoff(
        auditor=auditor,
        probe=ReadOnlyMexcProbe(private),
        pipeline=pipeline,
        clock_ms=iter((1000,1200)).__next__,
    )
    previous=cp()
    prior=asdict(previous)
    revisions=[dict(prior,write_reason="ENTRY_PROTECTED")]
    trailing=reconstruct_v40_trailing_after_restart(
        checkpoint_row=prior,
        revision_rows=revisions,
        observed_post_entry_low=D("86"),
        price_tick=D(".01"),
        signed_fill_time_verified=True,
        full_post_fill_low_stream_verified=True,
    )
    assert trailing.proposed_stop_price==a.proposed_stop
    return a,previous,trailing,handoff,journal,writer,venue,private


def commit(x):
    a,previous,trailing,handoff,*_=x
    return asyncio.run(handoff.commit_in_place_if_verified(
        amendment=a,previous=previous,trailing=trailing,permit=None,
    ))


def test_default_production_permit_denied_before_signed_get_or_db_write():
    x=fixture()
    with pytest.raises(LiveAuthorizationHalted):
        commit(x)
    assert x[4].states[x[0].external_id]=="UNKNOWN"
    assert x[5].calls==[]
    assert x[7].calls==[]


def authorize_mock_only(monkeypatch):
    # Only in unit tests; actual production typed permit remains disabled.
    monkeypatch.setattr(
        m,"require_certified_transport_permit",lambda permit:None,
    )


def test_mocked_in_place_readback_can_commit_journal_and_checkpoint_once(monkeypatch):
    authorize_mock_only(monkeypatch)
    x=fixture()
    outcome=commit(x)
    assert outcome.status==(
        "STOP_COMPLETION_JOURNAL_CHECKPOINT_PERSISTED_RESTART_REQUIRED"
    )
    assert outcome.journal_checkpoint_committed
    assert outcome.needs_post_commit_independent_restart_check
    assert outcome.exchange_posts==0
    assert not outcome.live_authorization
    assert not outcome.entry_allowed
    assert x[4].states[x[0].external_id]=="VERIFIED"
    assert x[4].verified[x[0].external_id]=="99"
    assert len(x[5].calls)==1
    old,new,reason=x[5].calls[0]
    assert reason=="STOP_VERIFIED"
    assert new.stop_price==D("86.5")
    assert new.checkpoint_version==2
    assert new.best_profit_pct==D("14")
    assert new.armed is True
    assert len(x[7].calls)==8
    assert all("private" in p for p in x[7].calls)


def test_exact_filled_entry_attached_stop_can_complete_checkpoint_without_post(monkeypatch):
    # This exercises the read-only recovery, independent signed GET,
    # and no-POST journal handoff using the SAME verified filled origin.
    from dataclasses import replace
    authorize_mock_only(monkeypatch)
    a,previous,trailing,handoff,journal,writer,venue,private=fixture()
    correct=replace(previous,exchange_order_id="666")
    handoff.auditor.ledger.rows[0]["exchange_order_id"]="666"
    venue.stops[0]["orderId"]="666"
    private.stop_rows[0]["orderId"]="666"
    revised_row=asdict(correct)
    trailing=reconstruct_v40_trailing_after_restart(
        checkpoint_row=revised_row,
        revision_rows=[dict(revised_row,write_reason="ENTRY_PROTECTED")],
        observed_post_entry_low=D("86"),
        price_tick=D(".01"),
        signed_fill_time_verified=True,
        full_post_fill_low_stream_verified=True,
    )
    out=asyncio.run(handoff.commit_in_place_if_verified(
        amendment=a,previous=correct,trailing=trailing,permit=None,
    ))
    assert out.journal_checkpoint_committed is True, out.status
    assert out.exchange_posts==0 and out.live_authorization is False
    assert len(writer.calls)==1


def test_unknown_unchanged_stop_cannot_persist_or_retry(monkeypatch):
    authorize_mock_only(monkeypatch)
    x=fixture(old_stop=True)
    out=commit(x)
    assert out.status=="STOP_COMPLETION_WHOLE_ACCOUNT_RECOVERY_UNVERIFIED_FREEZE"
    assert x[5].calls==[]
    assert x[7].calls==[]
    assert x[4].states[x[0].external_id]=="UNKNOWN"


def test_replacement_identity_cannot_use_in_place_completion_route(monkeypatch):
    authorize_mock_only(monkeypatch)
    x=fixture(replacement=True)
    out=commit(x)
    assert out.status=="STOP_COMPLETION_WHOLE_ACCOUNT_RECOVERY_UNVERIFIED_FREEZE"
    assert x[5].calls==[]


def test_signed_get_timeout_never_marks_amendment_verified(monkeypatch):
    authorize_mock_only(monkeypatch)
    x=fixture()
    x[7].bad=True
    out=commit(x)
    assert out.status=="STOP_COMPLETION_SIGNED_GET_FAILED_FREEZE"
    assert x[4].states[x[0].external_id]=="UNKNOWN"
    assert x[5].calls==[]


def test_incomplete_post_entry_tick_stream_never_completes(monkeypatch):
    authorize_mock_only(monkeypatch)
    x=list(fixture())
    from dataclasses import replace
    x[2]=replace(x[2],post_fill_low_stream_complete=False)
    with pytest.raises(
        m.V40StopCompletionHalted,match="TRAILING_CHECKPOINT_ORIGIN",
    ):
        commit(tuple(x))
    assert x[7].calls==[]


def test_mismatched_checkpoint_version_halts_before_exchange_read(monkeypatch):
    authorize_mock_only(monkeypatch)
    from dataclasses import replace
    x=list(fixture())
    x[2]=replace(x[2],checkpoint_version=3)
    with pytest.raises(m.V40StopCompletionHalted):
        commit(tuple(x))
    assert x[7].calls==[]


def test_signed_exchange_stops_wrong_volume_or_market_type(monkeypatch):
    authorize_mock_only(monkeypatch)
    for problem in ("bad_qty","limit_stop","wrong_error_code"):
        x=fixture()
        if problem=="bad_qty":x[7].stop_rows[0]["vol"]=D(1)
        if problem=="limit_stop":x[7].stop_rows[0]["stopLossType"]=1
        if problem=="wrong_error_code":x[7].stop_rows[0]["errorCode"]=5
        out=commit(x)
        assert out.status=="STOP_COMPLETION_SIGNED_NATIVE_FULL_COVERAGE_UNVERIFIED_FREEZE"
        assert x[5].calls==[]


def test_journal_checkpoint_write_failure_remains_verified_for_recovery(monkeypatch):
    authorize_mock_only(monkeypatch)
    x=fixture()
    x[5].fail=True
    out=commit(x)
    assert out.status=="STOP_COMPLETION_DURABLE_JOURNAL_OR_CHECKPOINT_UNVERIFIED_FREEZE"
    assert x[4].states[x[0].external_id]=="VERIFIED"
    assert x[5].calls==[]
    assert out.exchange_posts==0


def test_bookend_current_position_drift_rejected(monkeypatch):
    authorize_mock_only(monkeypatch)
    x=fixture()
    original=x[7]._private_get
    calls=[0]
    async def changed(path,params=None):
        value=await original(path,params)
        if path.endswith("open_positions"):
            calls[0]+=1
            if calls[0]==2:
                value[0]["holdVol"]="21"
        return value
    x[7]._private_get=changed
    out=commit(x)
    assert out.status in (
        "STOP_COMPLETION_SIGNED_NATIVE_FULL_COVERAGE_UNVERIFIED_FREEZE",
        "STOP_COMPLETION_BOOKEND_STALE_OR_DRIFT_FREEZE",
    )
    assert x[5].calls==[]


def test_signed_get_protective_volume_field_drift_freezes_before_db(monkeypatch):
    # profitLossVolType=SAME reads full volume from "vol", so a changed
    # supplementary stopLossVol can pass independent static coverage checks.
    # It must STILL freeze as raw signed venue evidence drift.
    authorize_mock_only(monkeypatch)
    x=fixture()
    original=x[7]._private_get
    calls=[0]

    async def drifted(path,params=None):
        rows=await original(path,params)
        if path.endswith("stoporder/open_orders"):
            calls[0]+=1
            if calls[0]==2:
                rows[0]["stopLossVol"]="19"
        return rows

    x[7]._private_get=drifted
    out=commit(x)
    assert out.status=="STOP_COMPLETION_BOOKEND_STALE_OR_DRIFT_FREEZE"
    assert x[4].states[x[0].external_id]=="UNKNOWN"
    assert x[5].calls==[]
    assert out.exchange_posts==0


def test_signed_get_unknown_stop_field_drift_blocks_checkpoint(monkeypatch):
    # A venue-introduced/changed stop field must never be silently omitted
    # from the stability witness merely because it isn't a known field.
    authorize_mock_only(monkeypatch)
    x=fixture()
    original=x[7]._private_get
    count=[0]

    async def drifted(path,params=None):
        rows=await original(path,params)
        if path.endswith("stoporder/open_orders"):
            count[0]+=1
            rows[0]["venueVersion"]=count[0]
        return rows

    x[7]._private_get=drifted
    out=commit(x)
    assert out.status=="STOP_COMPLETION_BOOKEND_STALE_OR_DRIFT_FREEZE"
    assert x[5].calls==[]


def test_signed_get_noncanonical_value_halts_without_leaking(monkeypatch):
    authorize_mock_only(monkeypatch)
    x=fixture()
    x[7].stop_rows[0]["sensitive_uncertain_field"]=object()
    out=commit(x)
    assert out.status=="STOP_COMPLETION_SIGNED_GET_FAILED_FREEZE"
    assert x[5].calls==[]
    assert out.exchange_posts==0


def test_durable_journal_changed_after_exchange_bookends_freezes(monkeypatch):
    authorize_mock_only(monkeypatch)
    x=fixture()
    original=x[7]._private_get
    count=[0]
    async def journal_raced(path,params=None):
        rows=await original(path,params)
        if path.endswith("stoporder/open_orders"):
            count[0]+=1
            if count[0]==2:
                # Emulate a second worker changing the journal AFTER the
                # original auditor's ledger+journal bookends.
                x[4].states[x[0].external_id]="VERIFIED"
        return rows
    x[7]._private_get=journal_raced
    out=commit(x)
    assert out.status=="STOP_COMPLETION_JOURNAL_DRIFT_AFTER_GET_FREEZE"
    assert x[5].calls==[]
    assert out.exchange_posts==0


def test_journal_read_unavailable_after_signed_bookends_freezes(monkeypatch):
    authorize_mock_only(monkeypatch)
    x=fixture()
    original=x[7]._private_get
    count=[0]
    async def raced(path,params=None):
        rows=await original(path,params)
        if path.endswith("stoporder/open_orders"):
            count[0]+=1
            if count[0]==2:
                async def failed(_):
                    raise OSError("PRIVATE_LEDGER_CONNECTION")
                x[4].state=failed
        return rows
    x[7]._private_get=raced
    out=commit(x)
    assert out.status=="STOP_COMPLETION_POST_GET_JOURNAL_UNAVAILABLE_FREEZE"
    assert x[5].calls==[]
    assert "PRIVATE_LEDGER_CONNECTION" not in out.status
