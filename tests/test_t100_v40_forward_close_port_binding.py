"""Actual full V40 runtime binds one same-account native-close finalizer."""
import pytest

from app.t100_live_four_slot_ledger_reader import PostgresFourSlotLedgerReader
from app.t100_live_four_slot_mexc_readonly import FourSlotMexcReadVenue
from app.t100_live_four_slot_snapshot_coordinator import FourSlotSnapshotResult
from app.t100_live_leader_fencing import LeaderLease
from app.t100_live_readonly_probe import ReadOnlyMexcProbe
from app.t100_live_position_accounting import PostgresFinalAccountingLedger
from app.t100_v40_at_entry_contract_specs import PostgresV40AtEntryContractSpecReader
from app.t100_v40_close_financial_artifact_staging import V40CloseOnlyFinancialArtifactStaging
from app.t100_v40_forward_close_finalizer import V40ForwardCloseFinalizer,V40ForwardCloseResult
from app.t100_v40_native_close_mismatch_recovery import V40NativeCloseMismatchRecovery
from app.t100_v40_native_close_release_handoff import V40NativeCloseReleaseHandoff
from app.t100_v40_portfolio_close_financial_reader import V40NativeCloseFinancialReadPort
from app.t100_v40_unreleased_verified_slot_release import PostgresV40VerifiedSlotRelease
from app import t100_v40_full_portfolio_runtime as runtime
from tests.test_t100_v40_full_portfolio_runtime import bundle


def build():
    args=bundle()
    probe=object.__new__(ReadOnlyMexcProbe)
    args["venue"].probe=probe
    native=object.__new__(V40NativeCloseMismatchRecovery)
    native.ledger=args["ledger"]
    native.venue=args["venue"]
    native.probe=probe
    financial=object.__new__(V40NativeCloseFinancialReadPort)
    financial.probe=probe
    archive=object.__new__(PostgresV40AtEntryContractSpecReader)
    archive.pool=args["ledger"].pool
    financial.entry_contracts=archive
    staging=object.__new__(V40CloseOnlyFinancialArtifactStaging)
    staging.ledger=args["ledger"]
    staging.native=native
    staging.finance=financial
    staging.writer_pool=object()
    account=object.__new__(PostgresFinalAccountingLedger)
    account.pool=staging.writer_pool
    writer=object.__new__(PostgresV40VerifiedSlotRelease)
    writer.pool=staging.writer_pool
    handoff=object.__new__(V40NativeCloseReleaseHandoff)
    handoff.writer=writer
    handoff.probe=probe
    finalizer=object.__new__(V40ForwardCloseFinalizer)
    finalizer.staging=staging
    finalizer.final_accounting=account
    finalizer.handoff=handoff
    args.update(financial_staging=staging,forward_close=finalizer)
    return args


@pytest.mark.asyncio
async def test_forward_close_only_routes_to_one_fenced_actual_finalizer():
    args=build()
    p=runtime.V40LivePortfolioPorts(**args)
    calls=[]
    async def run_once(*,snapshot,lease):
        calls.append((snapshot.status,lease.fencing_token))
        return V40ForwardCloseResult(
            status="V40_FORWARD_NATIVE_CLOSE_DURABLY_ACCOUNTED_AND_SLOT_RELEASED_REOBSERVE",
            position_id="17",
            entry_and_close_fills_persisted=True,
            funding_persisted=True,
            final_accounting_persisted=True,
            slot_release_committed=True,
            database_commit_may_have_occurred=True,
        )
    args["forward_close"].run_once=run_once
    snap=FourSlotSnapshotResult("UNOWNED_OR_MISSING_CHECKPOINTS")
    lease=LeaderLease("t100-live-executor","forward-holder",10,45)
    result=await p.finalize_verified_forward_native_close(
        snapshot=snap,lease=lease,
    )
    assert result.slot_release_committed is True
    assert result.entry_allowed is False
    assert result.post_release_new_account_get_required is True
    assert calls==[("UNOWNED_OR_MISSING_CHECKPOINTS",10)]
    with pytest.raises(runtime.V40RuntimeAssemblyHalted,match="PORTFOLIO_PERMIT"):
        await p.enter_one(permit=object())


@pytest.mark.parametrize("drift",[
    "wrong_account_sql","wrong_exchange_signer","wrong_staging","wrong_venue",
])
def test_wrong_account_or_signer_rejected_before_any_close(drift):
    args=build()
    if drift=="wrong_account_sql":
        args["forward_close"].final_accounting.pool=object()
    elif drift=="wrong_exchange_signer":
        args["forward_close"].handoff.probe=object.__new__(ReadOnlyMexcProbe)
    elif drift=="wrong_staging":
        args["forward_close"].staging=object()
    elif drift=="wrong_venue":
        args["financial_staging"].native.venue=object.__new__(FourSlotMexcReadVenue)
    with pytest.raises(runtime.V40RuntimeAssemblyHalted,match="PORTS_MISSING_OR_DIFFERENT_ACCOUNT"):
        runtime.V40LivePortfolioPorts(**args)


@pytest.mark.asyncio
async def test_unconnected_close_cannot_write_or_release():
    args=bundle()
    p=runtime.V40LivePortfolioPorts(**args)
    with pytest.raises(runtime.V40RuntimeAssemblyHalted,match="FORWARD_CLOSE_NOT_WIRED"):
        await p.finalize_verified_forward_native_close(
            snapshot=FourSlotSnapshotResult("UNOWNED_OR_MISSING_CHECKPOINTS"),
            lease=LeaderLease("t100-live-executor","forward-holder",10,45),
        )
    assert not args["release"].calls
