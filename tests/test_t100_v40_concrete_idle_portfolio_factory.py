"""Real account lifecycle can boot without fake Stage2 or order permit.

All ports are real types, same authenticated signer, source SELECT role,
immutable ledger SELECT role, and independent writer SQL pool. Graph
construction is pure: test dependencies explode if an exchange request,
SQL acquisition or mutation is attempted.
"""
from dataclasses import replace

import pytest

from app.t100_v40_concrete_idle_portfolio_factory import (
    V40IdlePortfolioAssemblyHalted,V40ConcreteIdlePortfolioGraph,
    assemble_concrete_v40_idle_portfolio_graph,
)
from app.t100_v40_full_portfolio_runtime import (
    V40LivePortfolioPorts,V40RuntimeAssemblyHalted,
)
from app.t100_v40_real_current_stop_preflight import V40RealCurrentStopPreflight
from app.t100_v40_forward_close_finalizer import V40ForwardCloseFinalizer
from app.t100_v40_portfolio_stop_supervisor_port import V40FourSlotStopLifecyclePort
from app.t100_v40_unchanged_bar_commit_port import PostgresV40UnchangedBarCommit
from app.t100_live_readonly_probe import ReadOnlyMexcProbe
from app.t100_live_four_slot_ledger_reader import PostgresFourSlotLedgerReader
from tests.test_t100_v40_concrete_portfolio_factory import fixture,Client,Pool


def built():
    args=fixture(occupied=0)
    for key in (
        "signal","context","entry_risk_witness","depth_witness",
        "current_stop_preflight",
    ):
        args.pop(key)
    graph=assemble_concrete_v40_idle_portfolio_graph(**args)
    return graph,args


def test_no_stage2_at_start_still_builds_every_real_management_port():
    g,args=built()
    assert type(g) is V40ConcreteIdlePortfolioGraph
    assert type(g.ports) is V40LivePortfolioPorts
    assert g.ports.signal_idle_entry_unbound is True
    assert g.ports.entry is None
    assert type(g.ports.stop) is V40FourSlotStopLifecyclePort
    assert type(g.ports.stop.preflight) is V40RealCurrentStopPreflight
    assert type(g.ports.cursor) is PostgresV40UnchangedBarCommit
    assert type(g.ports.forward_close) is V40ForwardCloseFinalizer
    assert g.source.pool is args["source_pool"]
    assert g.ledger.pool is args["ledger_pool"]
    assert g.ports.financial_staging.ledger is g.ledger
    assert g.ports.financial_staging.writer_pool is args["writer_pool"]
    assert g.ports.forward_close.final_accounting.pool is args["writer_pool"]
    assert g.ports.forward_close.handoff.writer.pool is args["writer_pool"]
    assert g.ports.forward_close.handoff.probe is args["probe"]
    assert g.ports.close.probe is args["probe"]
    assert g.ports.stop.client is args["client"]
    assert g.venue.probe is args["probe"]
    assert g.ports.stop.preflight.venue is g.venue
    assert g.ports.stop.preflight.snapshots is g.snapshots
    assert g.ports.cursor.reader is g.ledger
    assert g.ports.cursor.writer_pool is args["writer_pool"]
    assert g.ports.owned_bars.scanner_pool is args["source_pool"]
    assert not g.entry_allowed and not g.live_authorization
    assert g.exchange_posts==g.database_writes==0


@pytest.mark.asyncio
async def test_signal_idle_never_opens_even_with_fake_or_valid_permit():
    g,_=built()
    for candidate in (None,object()):
        with pytest.raises(
            V40RuntimeAssemblyHalted,match="SIGNAL_SPECIFIC_ENTRY_NOT_BOUND",
        ):
            await g.ports.enter_one(permit=candidate)
    assert g.ports.entry is None


@pytest.mark.parametrize("fault",[
    "reused_scanner_ledger","reused_writer_ledger","wrong_signer",
    "missing_signer","missing_writer","invalid_scope",
    "bad_public","invalid_caps","empty_holder",
])
def test_missing_port_or_wrong_identity_denies_boot(fault):
    args=fixture()
    for key in (
        "signal","context","entry_risk_witness","depth_witness",
        "current_stop_preflight",
    ):
        args.pop(key)
    if fault=="reused_scanner_ledger":
        args["ledger_pool"]=args["source_pool"]
    elif fault=="reused_writer_ledger":
        args["writer_pool"]=args["ledger_pool"]
    elif fault=="wrong_signer":
        args["probe"]=ReadOnlyMexcProbe(Client())
    elif fault=="missing_signer":
        args["probe"]=object()
    elif fault=="missing_writer":
        args["writer_pool"]=None
    elif fault=="invalid_scope":
        args["account_scope_sha256"]="not-a-real-sha256"
    elif fault=="bad_public":
        args["public_get"]=object()
    elif fault=="invalid_caps":
        args["caps"]=object()
    elif fault=="empty_holder":
        args["holder_id"]=""
    with pytest.raises(V40IdlePortfolioAssemblyHalted):
        assemble_concrete_v40_idle_portfolio_graph(**args)


def test_idle_requires_explicit_unbound_flag_in_original_runtime():
    g,args=built()
    # No other path can silently smuggle an unbound order adapter.
    with pytest.raises(V40RuntimeAssemblyHalted):
        V40LivePortfolioPorts(
            source=g.source,ledger=g.ledger,
            snapshots=g.snapshots,venue=g.venue,
            completeness=g.snapshots.completeness,
            flat_guard=g.ports.flat_guard,
            nonflat_guard=g.ports.nonflat_guard,
            owned_bars=g.ports.owned_bars,
            entry=None,stop=g.ports.stop,cursor=g.ports.cursor,
            close=g.ports.close,release=g.ports.release,
            financial_staging=g.ports.financial_staging,
            forward_close=g.ports.forward_close,
        )
