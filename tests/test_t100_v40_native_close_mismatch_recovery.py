"""After actual MEXC stop execution, OPEN SQL checkpoint remains until close.

Test one missing native position independently with 0..3 survivors, unknown
mismatches, ledger drift and venue drift. No SQL write or MEXC order POST.
"""
from copy import deepcopy
from dataclasses import replace
from decimal import Decimal as D
from types import SimpleNamespace

import pytest

from app.t100_live_durable_recovery import checkpoint_from_row
from app.t100_live_four_slot_ledger_reader import PostgresFourSlotLedgerReader
from app.t100_live_four_slot_mexc_readonly import FourSlotMexcReadVenue
from app.t100_live_four_slot_snapshot_coordinator import (
    FourSlotSnapshotResult,
)
from app.t100_live_readonly_probe import ReadOnlyMexcProbe
from app.t100_v40_unreleased_portfolio_native_close import V40PortfolioCloseWitness
from app import t100_v40_native_close_mismatch_recovery as recovery
from tests.test_t100_live_four_slot_snapshot_coordinator import fixture


def setup(n):
    original,_,positions,stops=fixture(n)
    probe=object.__new__(ReadOnlyMexcProbe)
    venue=FourSlotMexcReadVenue(probe)
    ledger=PostgresFourSlotLedgerReader(object())
    venue_reads=[]
    ledger_reads=[]
    remaining=deepcopy(positions[1:])
    remain_stops=deepcopy(stops[1:])
    async def get_positions():
        venue_reads.append("positions")
        return deepcopy(remaining)
    async def get_stops():
        venue_reads.append("stops")
        return deepcopy(remain_stops)
    async def get_ledger():
        ledger_reads.append("ledger")
        return deepcopy(original)
    venue.open_positions=get_positions
    venue.open_stop_orders=get_stops
    ledger.read_snapshot=get_ledger
    port=recovery.V40NativeCloseMismatchRecovery(
        ledger=ledger,venue=venue,probe=probe,
        independently_verified_account_scope_sha256="a"*64,
    )
    snap=FourSlotSnapshotResult("UNOWNED_OR_MISSING_CHECKPOINTS")
    cp=checkpoint_from_row(original.checkpoints[0])
    return SimpleNamespace(
        port=port,snapshot=snap,ledger=ledger,venue=venue,cp=cp,
        venue_reads=venue_reads,ledger_reads=ledger_reads,
        remaining=remaining,stops=remain_stops,
        original=original,
    )


def positive_for(closing,number):
    return V40PortfolioCloseWitness(
        status="V40_PORTFOLIO_CLOSE_EXCHANGE_SL_LABEL_OBSERVED_UNRELEASED",
        closed_position_id=closing.position_id,
        exact_close_order_id="55555",
        surviving_protected_slots=number-1,
        exact_closed_position_history_verified=True,
        portfolio_unchanged_across_bookends=True,
        closing_fills=SimpleNamespace(
            position_id=closing.position_id,
            symbol=closing.symbol,
            side="CLOSE_SHORT",
            deal_volume=closing.contracts,
            final_order_fill_history_complete=True,
            strategy_fully_filled_order_complete=True,
        ),
        native_terminal=SimpleNamespace(
            native_stop_terminal_verified=True,
            linked_venue_close_filled=True,
            owned_position_closed_verified=True,
        ),
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("n",[1,2,3,4])
async def test_one_real_close_matches_ledger_one_missing_even_between_candles(
    monkeypatch,n,
):
    state=setup(n)
    calls=[]
    async def native_get(*,probe,closing,account_scope_sha256,surviving):
        calls.append((probe,closing,account_scope_sha256,surviving))
        return positive_for(state.cp,n)
    monkeypatch.setattr(
        recovery,"observe_one_native_exit_among_v40_survivors",native_get
    )
    out=await state.port.observe(state.snapshot)
    assert out.status=="V40_EXACT_ONE_NATIVE_EXIT_POSITIVE_ACCOUNTING_AND_RELEASE_PENDING"
    assert out.previous==state.cp
    assert len(out.survivors)==n-1
    assert out.witness.closed_position_id==state.cp.position_id
    assert calls[0][0] is state.port.probe
    assert calls[0][1].position_id==state.cp.position_id
    assert calls[0][1].stop_order_id==state.cp.stop_order_id
    assert calls[0][2]=="a"*64
    assert state.ledger_reads==["ledger","ledger"]
    assert state.venue_reads==["positions","stops","positions","stops"]
    assert out.ledger_bookends_equal and out.venue_bookends_equal
    assert out.positive_native_close_verified
    assert not out.funding_and_final_accounting_verified
    assert not out.slot_release_permitted and not out.next_entry_allowed
    assert not out.live_authorization and out.mexc_posts==out.database_writes==0


@pytest.mark.asyncio
@pytest.mark.parametrize("drift",[
    "unknown_snapshot","foreign","none_missing","two_missing","orphan_stop",
    "duplicate_position","bad_ledger_revision","ledger_not_open",
])
async def test_any_ambiguous_exchange_or_ledger_refuses_before_history(
    monkeypatch,drift,
):
    state=setup(3)
    calls=[]
    async def native(*args,**kwargs):
        calls.append("SIGNED_NATIVE_TERMINAL")
        return positive_for(state.cp,3)
    monkeypatch.setattr(
        recovery,"observe_one_native_exit_among_v40_survivors",native
    )
    if drift=="unknown_snapshot":
        state.snapshot=replace(state.snapshot,status="INVENTORY_COMPLETENESS_UNVERIFIED")
    elif drift=="foreign":
        state.remaining[0]["positionId"]="99999"
    elif drift=="none_missing":
        state.remaining.append({"positionId":str(state.cp.position_id)})
        state.stops.append({"positionId":str(state.cp.position_id)})
    elif drift=="two_missing":
        state.remaining.pop()
        state.stops.pop()
    elif drift=="orphan_stop":
        state.stops[0]["positionId"]="99999"
    elif drift=="duplicate_position":
        state.remaining[1]["positionId"]=state.remaining[0]["positionId"]
    elif drift=="bad_ledger_revision":
        state.original=replace(state.original,readonly_repeatable_read=False)
        async def corrupted():return state.original
        state.ledger.read_snapshot=corrupted
    elif drift=="ledger_not_open":
        state.original.checkpoints[0]["lifecycle_state"]="CLOSED"
        async def corrupted():return state.original
        state.ledger.read_snapshot=corrupted
    with pytest.raises(recovery.V40NativeCloseMismatchHalted):
        await state.port.observe(state.snapshot)
    assert calls==[]


@pytest.mark.asyncio
async def test_second_venue_bookend_drift_fails_without_slot_release(monkeypatch):
    state=setup(2)
    async def native(**kwargs):return positive_for(state.cp,2)
    monkeypatch.setattr(
        recovery,"observe_one_native_exit_among_v40_survivors",native
    )
    calls=0
    async def changed():
        nonlocal calls
        calls+=1
        if calls==2:
            return []
        return deepcopy(state.remaining)
    state.venue.open_positions=changed
    with pytest.raises(
        recovery.V40NativeCloseMismatchHalted,match="CHANGED_DURING_GET",
    ):
        await state.port.observe(state.snapshot)


@pytest.mark.asyncio
async def test_second_ledger_bookend_drift_fails_without_slot_release(monkeypatch):
    state=setup(3)
    async def native(**kwargs):return positive_for(state.cp,3)
    monkeypatch.setattr(
        recovery,"observe_one_native_exit_among_v40_survivors",native
    )
    times=0
    async def changed():
        nonlocal times
        times+=1
        if times==2:
            return replace(state.original,checkpoints=state.original.checkpoints[:1])
        return state.original
    state.ledger.read_snapshot=changed
    with pytest.raises(
        recovery.V40NativeCloseMismatchHalted,match="CHANGED_DURING_GET",
    ):
        await state.port.observe(state.snapshot)


@pytest.mark.asyncio
@pytest.mark.parametrize("bad",[
    "missing_native","wrong_id","wrong_survivor_count",
    "missing_fills","missing_history","unknown_state",
])
async def test_missing_positive_native_terminal_never_frees_slot(monkeypatch,bad):
    state=setup(3)
    async def native(**kwargs):
        out=positive_for(state.cp,3)
        if bad=="missing_native":
            out=replace(out,native_terminal=None)
        elif bad=="wrong_id":
            out=replace(out,closed_position_id="999")
        elif bad=="wrong_survivor_count":
            out=replace(out,surviving_protected_slots=0)
        elif bad=="missing_fills":
            out=replace(out,closing_fills=None)
        elif bad=="missing_history":
            out=replace(out,exact_closed_position_history_verified=False)
        else:
            out=replace(out,status="UNKNOWN")
        return out
    monkeypatch.setattr(
        recovery,"observe_one_native_exit_among_v40_survivors",native
    )
    with pytest.raises(recovery.V40NativeCloseMismatchHalted):
        await state.port.observe(state.snapshot)


def test_cross_account_probe_and_missing_scope_rejected():
    state=setup(2)
    with pytest.raises(recovery.V40NativeCloseMismatchHalted):
        recovery.V40NativeCloseMismatchRecovery(
            ledger=state.ledger,venue=state.venue,
            probe=object.__new__(ReadOnlyMexcProbe),
            independently_verified_account_scope_sha256="a"*64,
        )
    with pytest.raises(recovery.V40NativeCloseMismatchHalted):
        recovery.V40NativeCloseMismatchRecovery(
            ledger=state.ledger,venue=state.venue,probe=state.port.probe,
            independently_verified_account_scope_sha256="INVALID",
        )
