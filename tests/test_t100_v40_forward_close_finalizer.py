"""Forward-live: one native SL -> exact fills/funding -> fenced CLOSE CAS.

All venue GET results use the independently exercised genuine typed
deal/paginated-funding fixture. SQL writes and exact native second readback are
injected *only* in this offline unit test. No real network or money.
"""
from dataclasses import replace
from types import SimpleNamespace

import pytest

from app.t100_live_four_slot_snapshot_coordinator import FourSlotSnapshotResult
from app.t100_live_leader_fencing import LeaderLease
from app.t100_live_position_accounting import PostgresFinalAccountingLedger
from app.t100_live_readonly_probe import ReadOnlyMexcProbe
from app.t100_v40_close_financial_artifact_staging import (
    V40CloseOnlyFinancialArtifactStaging,V40CloseArtifactReceipt,
)
from app.t100_v40_native_close_mismatch_recovery import (
    V40NativeCloseMismatchRecovery,V40NativeCloseMismatchEvidence,
)
from app.t100_v40_native_close_release_handoff import (
    V40NativeCloseReleaseHandoff,V40NativeCloseReleaseHandoffReport,
)
from app.t100_v40_unreleased_verified_slot_release import PostgresV40VerifiedSlotRelease
from app.t100_v40_at_entry_contract_specs import PostgresV40AtEntryContractSpecReader
from app import t100_v40_forward_close_finalizer as m
from tests.test_t100_v40_portfolio_close_financial_reader import (
    environment,archived_entry,
)


def setup(monkeypatch):
    previous,witness,finance,private_calls,funding_calls=environment(monkeypatch)
    pool=object()
    archive=object.__new__(PostgresV40AtEntryContractSpecReader)
    archive.pool=pool
    async def read(previous):
        return archived_entry(previous)
    archive.read=read
    finance.entry_contracts=archive
    native=object.__new__(V40NativeCloseMismatchRecovery)
    native.scope="a"*64
    native.probe=finance.probe
    n=V40NativeCloseMismatchEvidence(
        status="V40_EXACT_ONE_NATIVE_EXIT_POSITIVE_ACCOUNTING_AND_RELEASE_PENDING",
        previous=previous,survivors=(),witness=witness,
    )
    calls=[]
    async def observe(snapshot):
        calls.append("SIGNED_NATIVE_STOP_CLOSE")
        return n
    native.observe=observe
    stage=object.__new__(V40CloseOnlyFinancialArtifactStaging)
    stage.native=native
    stage.finance=finance
    stage.ledger=SimpleNamespace(pool=object())
    stage.writer_pool=pool
    async def stage_once(*,snapshot,lease):
        calls.append("FENCED_ATOMIC_FILL_AND_FUNDING_STAGING")
        return V40CloseArtifactReceipt(
            status="V40_CLOSE_FILLS_AND_FUNDING_DURABLY_STAGED_FINAL_ACCOUNTING_NOT_CERTIFIED",
            position_id=previous.position_id,
            immutable_entry_fills_verified=True,
            immutable_close_fills_verified=True,
            immutable_funding_verified=True,
            database_commit_may_have_occurred=True,
        )
    stage.stage_once=stage_once
    accounting=PostgresFinalAccountingLedger.__new__(PostgresFinalAccountingLedger)
    accounting.pool=pool
    async def persist(obj):
        calls.append("FINAL_ACCOUNTING_PERSISTED")
        assert obj.reconciliation.status=="CLOSED_RECONCILED"
        return "PERSISTED"
    accounting.persist=persist
    writer=PostgresV40VerifiedSlotRelease.__new__(PostgresV40VerifiedSlotRelease)
    writer.pool=pool
    handoff=V40NativeCloseReleaseHandoff.__new__(V40NativeCloseReleaseHandoff)
    handoff.probe=finance.probe
    handoff.writer=writer
    async def closed(**kwargs):
        calls.append("INDEPENDENT_NATIVE_GET_AND_FENCED_CAS_RELEASE")
        assert kwargs["accounting"].reconciliation.status=="CLOSED_RECONCILED"
        assert kwargs["closing"].position_id==previous.position_id
        assert kwargs["lease"].fencing_token==7
        return V40NativeCloseReleaseHandoffReport(
            status="V40_CLOSE_HANDOFF_ONE_SLOT_DURABLY_RELEASED_NEW_GET_REQUIRED",
            closed_position_id=previous.position_id,
            exact_exchange_close_verified=True,
            persisted_accounting_verified=True,
            durable_slot_release_verified=True,
            database_commit_may_have_occurred=True,
        )
    handoff.run_once=closed
    finalizer=m.V40ForwardCloseFinalizer(
        staging=stage,final_accounting=accounting,handoff=handoff,
    )
    async def staged_hashes(*,finance,lease):
        calls.append("IMMUTABLE_HASHES_AND_FENCE_INDEPENDENTLY_RECHECKED")
    finalizer._verify_staged_hashes=staged_hashes
    return SimpleNamespace(
        finalizer=finalizer,stage=stage,native=native,
        accounting=accounting,handoff=handoff,finance=finance,
        witness=witness,previous=previous,
        evidence=n,calls=calls,private_calls=private_calls,
        snapshot=FourSlotSnapshotResult("UNOWNED_OR_MISSING_CHECKPOINTS"),
        lease=LeaderLease("t100-live-executor","forward-unit-test",7,45),
    )


@pytest.mark.asyncio
async def test_real_typed_open_close_deals_and_funding_produce_atomic_slot_release(monkeypatch):
    x=setup(monkeypatch)
    result=await x.finalizer.run_once(snapshot=x.snapshot,lease=x.lease)
    assert result.status=="V40_FORWARD_NATIVE_CLOSE_DURABLY_ACCOUNTED_AND_SLOT_RELEASED_REOBSERVE"
    assert result.position_id==x.previous.position_id
    assert result.entry_and_close_fills_persisted
    assert result.funding_persisted and result.final_accounting_persisted
    assert result.slot_release_committed
    assert result.database_commit_may_have_occurred
    assert result.post_release_new_account_get_required
    assert not result.historical_contract_multiplier_continuity_certified
    assert not result.live_authorization and not result.entry_allowed
    assert result.mexc_posts==0
    assert x.calls==[
        "FENCED_ATOMIC_FILL_AND_FUNDING_STAGING",
        "SIGNED_NATIVE_STOP_CLOSE",
        "IMMUTABLE_HASHES_AND_FENCE_INDEPENDENTLY_RECHECKED",
        "FINAL_ACCOUNTING_PERSISTED",
        "INDEPENDENT_NATIVE_GET_AND_FENCED_CAS_RELEASE",
    ]
    assert ("order","100") in x.private_calls
    assert ("order","200") in x.private_calls


@pytest.mark.asyncio
@pytest.mark.parametrize("fault",[
    "wrong_lease","wrong_snapshot","mismatch_stage_receipt",
    "second_close_other_position","changed_signed_close_fills",
    "stale_durable_financial_hash","final_accounting_sql_unknown",
    "slot_release_sql_unknown","incorrect_release_ack",
])
async def test_ambiguous_reconciliation_stops_no_retries(monkeypatch,fault):
    x=setup(monkeypatch)
    if fault=="wrong_lease":
        x.lease=replace(x.lease,fencing_token=0)
    elif fault=="wrong_snapshot":
        x.snapshot=replace(x.snapshot,status="FLAT_OBSERVED_NONAUTHORIZING")
    elif fault=="mismatch_stage_receipt":
        async def badstage(**kwargs):
            return V40CloseArtifactReceipt(status="NOT_PERSISTED",position_id="10")
        x.stage.stage_once=badstage
    elif fault=="second_close_other_position":
        async def another(snapshot):
            return replace(x.evidence,previous=replace(x.previous,position_id="9999"))
        x.native.observe=another
    elif fault=="changed_signed_close_fills":
        async def corrupted(*,previous,witness):
            original=await type(x.finance).observe(x.finance,previous=previous,witness=witness)
            return replace(original,close_fills=replace(original.close_fills,fill_digest_sha256="0"*64))
        x.finance.observe=corrupted
    elif fault=="stale_durable_financial_hash":
        async def badhash(**kwargs):
            raise m.V40ForwardCloseHalted("V40_FORWARD_CLOSE_PERSISTED_FUNDING_DIGEST_DRIFT")
        x.finalizer._verify_staged_hashes=badhash
    elif fault=="final_accounting_sql_unknown":
        async def unknown(_accounting):
            raise TimeoutError("ambiguous db commit")
        x.accounting.persist=unknown
    elif fault=="slot_release_sql_unknown":
        async def unknown(**kwargs):
            raise TimeoutError("ambiguous slot release")
        x.handoff.run_once=unknown
    elif fault=="incorrect_release_ack":
        async def wrong(**kwargs):
            return V40NativeCloseReleaseHandoffReport(
                status="RELEASE_UNKNOWN",closed_position_id=x.previous.position_id,
            )
        x.handoff.run_once=wrong
    with pytest.raises(m.V40ForwardCloseHalted):
        await x.finalizer.run_once(snapshot=x.snapshot,lease=x.lease)
    assert x.calls.count("FENCED_ATOMIC_FILL_AND_FUNDING_STAGING")<=1
    assert x.calls.count("FINAL_ACCOUNTING_PERSISTED")<=1
    assert x.calls.count("INDEPENDENT_NATIVE_GET_AND_FENCED_CAS_RELEASE")<=1


def test_cross_account_probes_and_unbound_writer_refused(monkeypatch):
    x=setup(monkeypatch)
    x.handoff.probe=object.__new__(ReadOnlyMexcProbe)
    with pytest.raises(m.V40ForwardCloseHalted,match="SAME_ACCOUNT"):
        m.V40ForwardCloseFinalizer(
            staging=x.stage,final_accounting=x.accounting,handoff=x.handoff,
        )
