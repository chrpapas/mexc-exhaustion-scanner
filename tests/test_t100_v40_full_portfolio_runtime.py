"""True four-slot production port graph, same-account bindings and halt matrix.

Mocks are confined to this offline test. No private MEXC client or DB exists
here. No production release booleans are patched in repository code.
"""
import asyncio
from dataclasses import replace
from decimal import Decimal as D
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from app.t100_live_four_slot_ledger_reader import PostgresFourSlotLedgerReader
from app.t100_live_four_slot_mexc_readonly import (
    FourSlotMexcReadVenue, FourSlotMexcCompleteness,
)
from app.t100_live_four_slot_snapshot_coordinator import FourSlotSnapshotCoordinator
from app.t100_live_v40_entry_coordinator import V40OneShotCoordinator
from app.t100_live_v40_stage2_source import PostgresV40Stage2Source
from app.t100_live_execution import LiveCaps
from app.t100_v40_guardian_post_gate import PostgresV40GuardianPostGate
from app.t100_v40_unreleased_nonflat_guardian_evidence import (
    PostgresV40NonflatGuardianEvidence,
)
from app.t100_v40_owned_completed_bar_reader import PostgresV40OwnedBarReader
from app.t100_v40_nonflat_exit_frame_reader import V40RealNonflatExitFrameReader
from app.t100_v40_portfolio_order_transport import (
    V40FourSlotMexcOrderTransport,V40FourSlotPostGuard,
)
from app.t100_v40_portfolio_stop_supervisor_port import (
    V40FourSlotStopLifecyclePort,
)
from app import t100_v40_full_portfolio_runtime as runtime
from tests.test_t100_v40_live_portfolio_supervisor import (
    clean_flat,flat_guardian,owned_guardian,source,active,
)


class Cursor:
    def __init__(self):self.calls=[]
    async def commit(self,projection,*,permit):
        self.calls.append((projection,permit))
        return SimpleNamespace(status="CURSOR_SYNTHETIC")


class Close:
    def __init__(self):self.calls=[]
    async def observe(self,projection):
        self.calls.append(projection)
        return SimpleNamespace(status="NATIVE_CLOSE_READ_ONLY")


class Release:
    def __init__(self):self.calls=[]
    async def release(self,projection,witness,*,permit):
        self.calls.append((projection,witness,permit))
        return SimpleNamespace(status="CLOSE_RELEASE_SYNTHETIC")


def bundle():
    runtime_pool=object()
    scanner_pool=object()
    signer=object()

    src=object.__new__(PostgresV40Stage2Source)
    src.pool=scanner_pool
    ld=object.__new__(PostgresFourSlotLedgerReader)
    ld.pool=runtime_pool
    venue=object.__new__(FourSlotMexcReadVenue)
    complete=object.__new__(FourSlotMexcCompleteness)
    complete.venue=venue
    snapshots=FourSlotSnapshotCoordinator(
        ledger=ld,venue=venue,completeness=complete,
    )
    fg=object.__new__(PostgresV40GuardianPostGate)
    fg.pool=runtime_pool
    ng=object.__new__(PostgresV40NonflatGuardianEvidence)
    ng.pool=runtime_pool
    bars=object.__new__(PostgresV40OwnedBarReader)
    bars.runtime_pool=runtime_pool
    bars.scanner_pool=scanner_pool

    order=object.__new__(V40FourSlotMexcOrderTransport)
    order.client=signer
    guard=object.__new__(V40FourSlotPostGuard)
    guard.client=signer
    guard.ledger=ld
    order.guard=guard

    entry=object.__new__(V40OneShotCoordinator)
    entry.source=src
    entry.snapshots=snapshots
    entry.get_venue=venue
    entry.completeness=complete
    entry.initial_pilot_only=False
    entry.transport=order

    stop=object.__new__(V40FourSlotStopLifecyclePort)
    stop.ledger=ld
    stop.client=signer

    return dict(
        source=src,ledger=ld,snapshots=snapshots,venue=venue,
        completeness=complete,flat_guard=fg,nonflat_guard=ng,
        owned_bars=bars,entry=entry,stop=stop,
        cursor=Cursor(),close=Close(),release=Release(),
    )


def build(**changes):
    d=bundle()
    d.update(changes)
    return runtime.V40LivePortfolioPorts(**d)


def test_unpatched_release_gate_rejects_before_any_mutation():
    ports=build()
    with pytest.raises(
        runtime.V40RuntimeAssemblyHalted,match="PORTFOLIO_PERMIT_NOT_CERTIFIED",
    ):
        asyncio.run(ports.persist_unchanged_stop_checkpoint(
            object(),permit=object(),
        ))
    assert not ports.cursor.calls


@pytest.mark.parametrize("missing",["cursor","close","release"])
def test_cannot_assemble_without_all_mutation_and_accounting_paths(missing):
    with pytest.raises(
        runtime.V40RuntimeAssemblyHalted,match="PORTS_MISSING",
    ):
        build(**{missing:None})


@pytest.mark.parametrize("drift",[
    "canary","missing_stop_signer","different_exchange_signer",
    "wrong_entry_source","wrong_snapshot","wrong_runtime_ledger",
    "wrong_guard_pool","wrong_scanner_pool","wrong_venue","wrong_completeness",
])
def test_no_cross_subaccount_or_canary_or_sql_role_fallback(drift):
    d=bundle()
    if drift=="canary":
        d["entry"].initial_pilot_only=True
    elif drift=="missing_stop_signer":
        d["stop"].ledger=object.__new__(PostgresFourSlotLedgerReader)
    elif drift=="different_exchange_signer":
        d["stop"].client=object()
    elif drift=="wrong_entry_source":
        d["entry"].source=object.__new__(PostgresV40Stage2Source)
    elif drift=="wrong_snapshot":
        d["entry"].snapshots=object.__new__(FourSlotSnapshotCoordinator)
    elif drift=="wrong_runtime_ledger":
        d["snapshots"].ledger=object.__new__(PostgresFourSlotLedgerReader)
    elif drift=="wrong_guard_pool":
        d["flat_guard"].pool=object()
    elif drift=="wrong_scanner_pool":
        d["owned_bars"].scanner_pool=object()
    elif drift=="wrong_venue":
        d["snapshots"].venue=object.__new__(FourSlotMexcReadVenue)
    elif drift=="wrong_completeness":
        d["snapshots"].completeness=object.__new__(FourSlotMexcCompleteness)
    with pytest.raises(runtime.V40RuntimeAssemblyHalted):
        runtime.V40LivePortfolioPorts(**d)


@pytest.mark.asyncio
async def test_flat_observer_is_live_source_not_paper_placeholder(monkeypatch):
    d=bundle()
    p=runtime.V40LivePortfolioPorts(**d)
    # Capture before patching the coordinator; otherwise clean_flat()
    # recursively invokes the mock inspect() instead of a real synthetic
    # ledger/venue coordinator.
    flat_snapshot=await clean_flat()
    async def inspect(self,**kwargs):return flat_snapshot
    async def guardian(self):return flat_guardian()
    async def latest(self,*,observed_at):return source(False)
    monkeypatch.setattr(FourSlotSnapshotCoordinator,"inspect",inspect)
    monkeypatch.setattr(PostgresV40GuardianPostGate,"verify",guardian)
    monkeypatch.setattr(PostgresV40Stage2Source,"latest_candidates",latest)
    frame=await p.observe()
    assert frame.snapshot.status=="FLAT_OBSERVED_NONAUTHORIZING"
    assert frame.guardian.critical_latch_absent
    assert frame.source.candidates==()
    assert frame.completed_exit is None


@pytest.mark.asyncio
async def test_nonflat_owner_observer_uses_same_native_stop_and_cursor_reader(monkeypatch):
    d=bundle()
    p=runtime.V40LivePortfolioPorts(**d)
    f=active(2,low="86")
    async def inspect(self,**kwargs):return f.snapshot
    async def real(self,**kwargs):
        assert kwargs["ledger"] is p.ledger
        assert kwargs["venue"] is p.venue
        assert kwargs["source"] is p.source
        return f
    monkeypatch.setattr(FourSlotSnapshotCoordinator,"inspect",inspect)
    monkeypatch.setattr(V40RealNonflatExitFrameReader,"observe",real)
    frame=await p.observe()
    assert frame is f
    assert frame.snapshot.occupied_slots==2


@pytest.mark.asyncio
async def test_production_ports_route_one_action_and_separate_readonly_close(monkeypatch):
    p=build()
    pmt=SimpleNamespace(scope="T100_V40_FOUR_SLOT_PRODUCTION_TRIAL")
    monkeypatch.setattr(runtime,"require_certified_transport_permit",lambda x:x)
    recorded=[]
    async def entry(self,**kwargs):
        recorded.append(("entry",kwargs))
        return "ENTRY_SYNTHETIC"
    async def stop(self,projection,*,permit):
        recorded.append(("stop",projection,permit))
        return "STOP_SYNTHETIC"
    monkeypatch.setattr(V40OneShotCoordinator,"run_one",entry)
    monkeypatch.setattr(V40FourSlotStopLifecyclePort,"tighten_one_stop",stop)
    assert await p.enter_one(permit=pmt,monitored_signal_id=497)=="ENTRY_SYNTHETIC"
    assert (await p.tighten_one_stop("completed-bar",permit=pmt))=="STOP_SYNTHETIC"
    witness=await p.reconcile_native_close("bar-close")
    assert witness.status=="NATIVE_CLOSE_READ_ONLY"
    assert p.close.calls==["bar-close"]
    await p.persist_unchanged_stop_checkpoint("bar",permit=pmt)
    await p.release_verified_native_close("close",witness,permit=pmt)
    assert p.cursor.calls==[("bar",pmt)]
    assert p.release.calls==[("close",witness,pmt)]
    assert len(recorded)==2


@pytest.mark.asyncio
async def test_canary_permit_cannot_promote_any_live_port(monkeypatch):
    p=build()
    canary=SimpleNamespace(scope="T100_V40_SUPERVISED_FIRST_SIGNAL")
    monkeypatch.setattr(runtime,"require_certified_transport_permit",lambda x:x)
    for action in (
        p.enter_one(permit=canary),
        p.tighten_one_stop("bar",permit=canary),
        p.persist_unchanged_stop_checkpoint("bar",permit=canary),
        p.release_verified_native_close("bar","witness",permit=canary),
    ):
        with pytest.raises(runtime.V40RuntimeAssemblyHalted):
            await action
    assert not p.cursor.calls and not p.release.calls


def test_supervisor_assembly_rejects_tiny_canary_caps():
    p=build()
    with pytest.raises(runtime.V40RuntimeAssemblyHalted,
                       match="CANNOT_ADOPT_SINGLE_CANARY_CAPS"):
        runtime.assemble_full_v40_supervisor(
            p,caps=LiveCaps(D("10"),D("40"),D("40")),
            parity_report={},holder_id="v40-test",
        )
    cap=LiveCaps(
        D("10"),D("40"),D("40"),leverage=3,
        risk_profile="T100_V40_FOUR_SLOT_PRODUCTION_TRIAL",
    )
    supervisor=runtime.assemble_full_v40_supervisor(
        p,caps=cap,parity_report={},holder_id="v40-four-slot",
    )
    assert supervisor.ports is p
    assert supervisor.caps.leverage==3


@pytest.mark.asyncio
async def test_intrabar_native_exit_inventory_mismatch_requires_positive_readback(
    monkeypatch,
):
    """A successful native stop removes the exchange slot before SQL CLOSED."""
    from app.t100_live_four_slot_snapshot_coordinator import FourSlotSnapshotResult
    from app.t100_live_readonly_probe import ReadOnlyMexcProbe
    from app.t100_v40_portfolio_positive_native_close_reader import (
        V40PositiveNativeCloseReadPort,
    )
    from app.t100_v40_native_close_mismatch_recovery import (
        V40NativeCloseMismatchRecovery,
    )
    d=bundle()
    probe=object.__new__(ReadOnlyMexcProbe)
    d["venue"].probe=probe
    close=object.__new__(V40PositiveNativeCloseReadPort)
    close.probe=probe
    close.ledger=d["ledger"]
    close.scope="a"*64
    d["close"]=close
    p=runtime.V40LivePortfolioPorts(**d)
    calls=[]
    async def mismatch(self,snap):
        calls.append((self.ledger,self.venue,self.probe,self.scope,snap.status))
        return SimpleNamespace(
            status="V40_EXACT_ONE_NATIVE_EXIT_POSITIVE_ACCOUNTING_AND_RELEASE_PENDING",
            positive_native_close_verified=True,
            slot_release_permitted=False,live_authorization=False,
        )
    async def inspect(self,**kwargs):
        return FourSlotSnapshotResult("UNOWNED_OR_MISSING_CHECKPOINTS")
    monkeypatch.setattr(FourSlotSnapshotCoordinator,"inspect",inspect)
    monkeypatch.setattr(V40NativeCloseMismatchRecovery,"observe",mismatch)
    with pytest.raises(
        runtime.V40RuntimeAssemblyHalted,
        match="NATIVE_EXIT_POSITIVE_SETTLEMENT_REQUIRED",
    ):
        await p.observe()
    assert calls==[(
        d["ledger"],d["venue"],probe,"a"*64,
        "UNOWNED_OR_MISSING_CHECKPOINTS",
    )]
    assert not p.entry.transport.guard.client is None


@pytest.mark.asyncio
async def test_native_exit_mismatch_never_uses_generic_close_or_unbound_probe(
    monkeypatch,
):
    from app.t100_live_four_slot_snapshot_coordinator import FourSlotSnapshotResult
    d=bundle()
    p=runtime.V40LivePortfolioPorts(**d)
    async def inspect(self,**kwargs):
        return FourSlotSnapshotResult("UNOWNED_OR_MISSING_CHECKPOINTS")
    monkeypatch.setattr(FourSlotSnapshotCoordinator,"inspect",inspect)
    with pytest.raises(
        runtime.V40RuntimeAssemblyHalted,
        match="NATIVE_CLOSE_SAME_ACCOUNT_PORT_MISSING",
    ):
        await p.observe()
    assert p.close.calls==[]
    assert p.release.calls==[]
    assert p.cursor.calls==[]
