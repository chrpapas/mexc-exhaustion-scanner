"""Owned V40 terminal native-close readback from ledger and signed venue.

Synthetic only. Actual native order/deal/funding finality is covered in
the existing underlying observer tests. No MEXC POST or DB write.
"""
import asyncio
from dataclasses import replace
from unittest.mock import patch
import pytest

from app.t100_live_four_slot_ledger_reader import PostgresFourSlotLedgerReader
from app.t100_live_readonly_probe import ReadOnlyMexcProbe
from app.t100_v40_unreleased_portfolio_native_close import V40PortfolioCloseWitness
from app import t100_v40_portfolio_positive_native_close_reader as m
from tests.test_t100_v40_live_portfolio_supervisor import active
from tests.test_t100_v40_unreleased_verified_exit_handoff import bundle


# The reused audit fixtures call asyncio.run() to build complete simulated
# ledgers. Materialize them once at import time, never inside pytest's
# asyncio event loop.
_EXITS={
    n:active(n,due_indices=(0,),high="180",low="80").completed_exit
    for n in (1,2,3,4)
}
_LEDGERS={n:bundle(n)["ledger"] for n in (1,2,3,4)}


def fixture(n=3):
    p=_EXITS[n]
    ledger=_LEDGERS[n]
    reader=object.__new__(PostgresFourSlotLedgerReader)
    probe=object.__new__(ReadOnlyMexcProbe)
    observed=[]
    async def get_ledger():
        observed.append("READ_ONLY_RESTRICTED_LEDGER")
        return ledger
    reader.read_snapshot=get_ledger
    port=m.V40PositiveNativeCloseReadPort(
        probe=probe,ledger=reader,
        independently_verified_account_scope_sha256="a"*64,
    )
    return p,ledger,port,observed


def positive(closed_id,n):
    return V40PortfolioCloseWitness(
        "V40_PORTFOLIO_CLOSE_EXCHANGE_TP_LABEL_CONFLICT_UNRELEASED",
        closed_position_id=closed_id,
        surviving_protected_slots=n-1,
        exact_closed_position_history_verified=True,
        portfolio_unchanged_across_bookends=True,
        closing_fills=object(),native_terminal=object(),
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("n",[1,2,3,4])
async def test_one_due_close_reads_exact_immutable_ledger_and_original_native_stop(
    monkeypatch,n,
):
    projection,ledger,port,readings=fixture(n)
    cp=ledger.checkpoints[0]
    called=[]
    async def observe(**kwargs):
        called.append(kwargs)
        return positive(str(cp["position_id"]),n)
    monkeypatch.setattr(m,"observe_one_native_exit_among_v40_survivors",observe)
    w=await port.observe(projection)
    assert w.closed_position_id==str(cp["position_id"])
    assert len(called)==1
    owned=called[0]["closing"]
    assert owned.position_id==str(cp["position_id"])
    assert owned.symbol==cp["symbol"]
    assert owned.stop_order_id==str(cp["stop_order_id"])
    assert owned.scope_sha256=="a"*64
    assert len(called[0]["surviving"])==n-1
    assert readings==["READ_ONLY_RESTRICTED_LEDGER"]*2
    assert w.live_authorization is False and not w.slot_released
    assert w.database_writes==w.mexc_posts==0


@pytest.mark.asyncio
async def test_no_candle_close_identity_no_exchange_calls(monkeypatch):
    p,ledger,port,reads=fixture(3)
    bad=replace(p,native_close_reconciliation_count=0)
    calls=[]
    async def should_not_call(**kwargs):
        calls.append(1)
        raise AssertionError("EXCHANGE_CLOSE_SHOULD_NEVER_BE_QUERIED")
    monkeypatch.setattr(m,"observe_one_native_exit_among_v40_survivors",
                        should_not_call)
    with pytest.raises(m.V40PositiveCloseReadHalted,match="ONE_CANDLE_CANDIDATE_REQUIRED"):
        await port.observe(bad)
    assert not calls and not reads


@pytest.mark.asyncio
async def test_historical_ledger_changes_during_exchange_GET_halts(monkeypatch):
    projection,ledger,port,reads=fixture(2)
    first=ledger
    second=replace(ledger,checkpoints=ledger.checkpoints[:-1])
    calls=0
    async def changing():
        nonlocal calls
        calls+=1
        return first if calls==1 else second
    port.ledger.read_snapshot=changing
    async def observe(**kwargs):
        return positive(str(first.checkpoints[0]["position_id"]),2)
    monkeypatch.setattr(m,"observe_one_native_exit_among_v40_survivors",observe)
    with pytest.raises(m.V40PositiveCloseReadHalted,match="LEDGER_CHANGED_DURING_EXCHANGE_HISTORY"):
        await port.observe(projection)
    assert calls==2


@pytest.mark.asyncio
async def test_positive_wrong_position_or_missing_terminal_does_not_release(monkeypatch):
    projection,ledger,port,reads=fixture(3)
    async def wrong(**kwargs):
        return positive("999999",3)
    monkeypatch.setattr(m,"observe_one_native_exit_among_v40_survivors",wrong)
    with pytest.raises(m.V40PositiveCloseReadHalted,match="WITNESS_IDENTITY"):
        await port.observe(projection)
    async def incomplete(**kwargs):
        return replace(positive(str(ledger.checkpoints[0]["position_id"]),3),
                       native_terminal=None)
    monkeypatch.setattr(m,"observe_one_native_exit_among_v40_survivors",incomplete)
    with pytest.raises(m.V40PositiveCloseReadHalted,match="WITNESS_IDENTITY"):
        await port.observe(projection)


@pytest.mark.asyncio
async def test_actual_signed_get_exception_is_redacted_and_does_not_write(monkeypatch):
    projection,ledger,port,reads=fixture(4)
    async def fail(**kwargs):
        raise RuntimeError("EXCHANGE PRIVATE KEY IN EXCEPTION")
    monkeypatch.setattr(m,"observe_one_native_exit_among_v40_survivors",fail)
    with pytest.raises(m.V40PositiveCloseReadHalted,match="EXCHANGE_OR_LEDGER_READ_FAILED") as info:
        await port.observe(projection)
    assert "PRIVATE KEY" not in str(info.value)
    assert reads==["READ_ONLY_RESTRICTED_LEDGER"]


def test_constructor_requires_signed_probe_and_account_scope():
    _,ledger,port,_=fixture()
    with pytest.raises(m.V40PositiveCloseReadHalted,match="SAME_ACCOUNT"):
        m.V40PositiveNativeCloseReadPort(
            probe=object(),ledger=port.ledger,
            independently_verified_account_scope_sha256="not-hash",
        )
