"""Concrete production dependency factory: no fake risk/stop grants or orders.

These unit tests exercise actual constructors and same-object account,
ledger, writer, leader, entry, native-stop, bar cursor, funding/final-PnL,
and per-signal transport bindings. The transport is never invoked.
"""
from dataclasses import replace
from decimal import Decimal as D
from types import SimpleNamespace

import pytest

from app.t100_live_execution import LiveCaps
from app.t100_live_readonly_probe import ReadOnlyMexcProbe
from app.t100_v40_exchange_leverage_plan import (
    V40CrossLeveragePlan,STATUS_FEASIBLE,
)
from app.t100_v40_portfolio_order_transport import V40PortfolioPostContext
from app.t100_v40_concrete_portfolio_factory import (
    V40ConcreteAssemblyHalted,assemble_concrete_v40_signal_graph,
    V40CandleNativeReleaseBridge,
)
from app.t100_v40_full_portfolio_runtime import V40LivePortfolioPorts
from app.t100_v40_live_portfolio_supervisor import V40PortfolioSupervisor
from app.t100_v40_portfolio_stop_supervisor_port import V40FourSlotStopLifecyclePort
from app.t100_v40_unchanged_bar_commit_port import PostgresV40UnchangedBarCommit
from app.t100_v40_forward_close_finalizer import V40ForwardCloseFinalizer
from app.t100_live_v40_entry_coordinator import V40OneShotCoordinator
from tests.test_t100_live_v40_entry_coordinator import candidate


class Pool:
    def acquire(self):
        raise AssertionError("graph assembly must NOT connect to a DB")
    async def fetchrow(self,*args):
        raise AssertionError("graph assembly must NOT write a DB")


class Client:
    async def _private_get(self,*args,**kwargs):
        raise AssertionError("graph assembly must NOT send venue GET")
    async def _private_post(self,*args,**kwargs):
        raise AssertionError("graph assembly must NOT submit orders")


class Public:
    async def get(self,*args,**kwargs):
        raise AssertionError("graph assembly must NOT query public orderbook")


class Risk:
    async def inspect(self,signal):
        raise AssertionError("risk collector only runs on deliberate entry")


class Depth:
    async def quote_exact_intent(self,signal,intent,*,max_slippage_bps):
        raise AssertionError("sized VWAP only runs during deliberate entry")


class StopPreflight:
    async def observe(self,pid):
        raise AssertionError("stop preflight only runs on completed Min30 exit")


def fixture(*,occupied=0):
    signal=candidate()
    client=Client()
    plan=V40CrossLeveragePlan(
        STATUS_FEASIBLE,
        signal_id=signal.signal_id,symbol=signal.symbol,
        exact_order_notional_usdt=D("100"),
        portfolio_gross_after_entry_usdt=D("100"),
        observed_equity_usdt=D("50"),
        selected_exchange_cross_leverage=2,
        nominal_initial_margin_fits=True,
        frozen_strategy_notional_unchanged=True,
        account_observations_verified=True,
    )
    ctx=V40PortfolioPostContext(
        expected_occupied_slots=occupied,
        expected_owned_position_ids=tuple(str(i+1) for i in range(occupied)),
        frozen_order_notional_usdt=D("100"),
        frozen_portfolio_gross_after_entry_usdt=D("100"),
        frozen_equity_at_plan_usdt=D("50"),
        protected_remaining_margin_headroom_usdt=D("0"),
        fee_reserve_usdt=D("0"),
        leverage_plan=plan,
    )
    return dict(
        source_pool=Pool(),ledger_pool=Pool(),writer_pool=Pool(),
        client=client,probe=ReadOnlyMexcProbe(client),public_get=Public(),
        account_scope_sha256="a"*64,
        signal=signal,context=ctx,
        entry_risk_witness=Risk(),depth_witness=Depth(),
        current_stop_preflight=StopPreflight(),
        caps=LiveCaps(
            max_order_usdt=D("500"),max_portfolio_usdt=D("1000"),
            max_daily_loss_usdt=D("100"),
            leverage=2,risk_profile="T100_V40_FOUR_SLOT_PRODUCTION_TRIAL",
        ),
        parity_report={},holder_id="v40-four-slot-mexc-forward",
        now_ms=lambda:1,
    )


@pytest.mark.parametrize("occupied",[0,1,2,3])
def test_concrete_all_ports_same_signer_all_four_occupancy_states(occupied):
    args=fixture(occupied=occupied)
    assembled=assemble_concrete_v40_signal_graph(**args)
    p=assembled.ports
    assert type(p) is V40LivePortfolioPorts
    assert type(assembled.supervisor) is V40PortfolioSupervisor
    assert type(assembled.entry) is V40OneShotCoordinator
    assert type(assembled.stop) is V40FourSlotStopLifecyclePort
    assert type(assembled.cursor) is PostgresV40UnchangedBarCommit
    assert type(assembled.settlement) is V40ForwardCloseFinalizer
    assert type(p.release) is V40CandleNativeReleaseBridge
    assert p.entry.transport.client is args["client"]
    assert p.stop.client is args["client"]
    assert p.venue.probe is args["probe"]
    assert p.entry.transport.guard.ledger is assembled.ledger
    assert p.entry.transport.context is args["context"]
    assert p.entry.transport.context.expected_occupied_slots==occupied
    assert p.entry.witness is args["entry_risk_witness"]
    assert p.entry.depth is args["depth_witness"]
    assert p.stop.preflight is args["current_stop_preflight"]
    assert p.entry.leaders is assembled.leaders
    assert p.entry.reservations.pool is args["writer_pool"]
    assert p.entry.journal.pool is args["writer_pool"]
    assert p.entry.claims.pool is args["writer_pool"]
    assert p.entry.claim_reader.pool is args["ledger_pool"]
    assert p.entry.commit_writer.pool is args["writer_pool"]
    assert p.cursor.writer_pool is args["writer_pool"]
    assert p.cursor.reader.pool is args["ledger_pool"]
    assert p.financial_staging.writer_pool is args["writer_pool"]
    assert p.financial_staging.ledger.pool is args["ledger_pool"]
    assert p.forward_close.final_accounting.pool is args["writer_pool"]
    assert p.forward_close.handoff.writer.pool is args["writer_pool"]
    assert p.forward_close.handoff.probe is args["probe"]
    assert p.close.probe is args["probe"]
    assert p.close.scope=="a"*64
    assert assembled.signal_id==args["signal"].signal_id
    assert not assembled.live_authorization
    assert assembled.exchange_posts==0==assembled.database_writes


@pytest.mark.parametrize("fault",[
    "same_sql_pool","same_writer_pool","foreign_venue_get",
    "missing_writer","missing_account_scope","wrong_signal_context",
    "wrong_signal_symbol","missing_stop_preflight",
    "missing_risk_witness","missing_depth","incorrect_slot_count",
])
def test_graph_rejects_foreign_identity_or_missing_required_binding(fault):
    args=fixture()
    if fault=="same_sql_pool":
        args["ledger_pool"]=args["source_pool"]
    elif fault=="same_writer_pool":
        args["writer_pool"]=args["ledger_pool"]
    elif fault=="foreign_venue_get":
        args["probe"]=ReadOnlyMexcProbe(Client())
    elif fault=="missing_writer":
        args["writer_pool"]=None
    elif fault=="missing_account_scope":
        args["account_scope_sha256"]="not-a-verified-account"
    elif fault=="wrong_signal_context":
        args["context"]=replace(
            args["context"],leverage_plan=replace(
                args["context"].leverage_plan,signal_id=999,
            ),
        )
    elif fault=="wrong_signal_symbol":
        args["context"]=replace(
            args["context"],leverage_plan=replace(
                args["context"].leverage_plan,symbol="OTHER_USDT",
            ),
        )
    elif fault=="missing_stop_preflight":
        args["current_stop_preflight"]=object()
    elif fault=="missing_risk_witness":
        args["entry_risk_witness"]=object()
    elif fault=="missing_depth":
        args["depth_witness"]=object()
    elif fault=="incorrect_slot_count":
        args["context"]=replace(
            args["context"],expected_occupied_slots=3,
        )
    with pytest.raises(V40ConcreteAssemblyHalted):
        assemble_concrete_v40_signal_graph(**args)


def test_per_signal_context_cannot_be_carried_to_next_stage2_symbol():
    args=fixture()
    from dataclasses import replace as r
    args["signal"]=r(args["signal"],signal_id=101,symbol="FRESH_USDT")
    with pytest.raises(V40ConcreteAssemblyHalted):
        assemble_concrete_v40_signal_graph(**args)


@pytest.mark.asyncio
async def test_candle_touch_without_portfolio_permit_cannot_release():
    args=fixture()
    graph=assemble_concrete_v40_signal_graph(**args)
    out=await graph.ports.release.release(
        projection=object(),witness=object(),permit=object(),
    )
    assert out.status.startswith("V40_CANDLE_CLOSE_NOT_PROVEN")
    assert out.durable_slot_release_verified is False
    assert out.mexc_posts==0
