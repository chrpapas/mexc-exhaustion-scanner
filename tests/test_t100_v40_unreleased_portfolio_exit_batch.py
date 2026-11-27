"""Frozen four-slot exit policy is all-or-nothing and nonauthorizing."""
from dataclasses import replace
from decimal import Decimal as D

import pytest

from app.t100_v40_unreleased_exit_policy import (
    V40CompletedBar,V40ExitCheckpoint,
)
from app.t100_v40_unreleased_portfolio_exit_batch import (
    V40PortfolioExitBatchHalted,project_v40_portfolio_exit_batch,
)

T=1_801_800
ENTRY=1_800_001


def checkpoint(i):
    return V40ExitCheckpoint(
        position_id=str(100+i),symbol=f"ALT{i}_USDT",
        entry_price=D("100"),filled_at_ts=ENTRY,
        native_stop_price=D("175"),
    )


def candle(*, high="100",low="95",close="96",open_="100", ts=T):
    return V40CompletedBar(
        open_ts=ts,open=D(open_),high=D(high),low=D(low),close=D(close),
    )


def project(checkpoints=None,bars=None,ticks=None,now=None):
    if checkpoints is None:
        checkpoints=tuple(checkpoint(i) for i in range(4))
    if bars is None:
        bars={c.symbol:candle() for c in checkpoints}
    if ticks is None:
        ticks={c.symbol:D(".01") for c in checkpoints}
    return project_v40_portfolio_exit_batch(
        checkpoints=checkpoints,completed_bars_by_symbol=bars,
        price_ticks_by_symbol=ticks,now_ts=now if now is not None else T+1800,
    )


def test_one_trigger_with_other_three_survivors_not_closed_or_released():
    cps=tuple(checkpoint(i) for i in range(4))
    bars={
        cps[0].symbol:candle(high="175",low="70",close="100"),
        cps[1].symbol:candle(low="86",close="86"),
        cps[2].symbol:candle(low="95",close="96"),
        cps[3].symbol:candle(low="80",close="80"),
    }
    r=project(cps,bars)
    assert r.status=="V40_PORTFOLIO_NATIVE_EXIT_UNVERIFIED_RECONCILE_ONLY"
    assert r.native_close_reconciliation_count==1
    assert r.proposed_stop_amendment_count==2
    assert r.unchanged_verified_stop_count==1
    assert r.slots[0].requires_native_order_reconciliation
    assert r.slots[0].decision.model_exit_price==D("175")
    assert r.slots[1].proposed_stop_price==D("86.50")
    assert r.slots[3].proposed_stop_price==D("80.50")
    assert all(not s.position_closed_verified for s in r.slots)
    assert all(not s.durable_checkpoint_committed for s in r.slots)
    assert all(not s.stop_mutation_verified for s in r.slots)
    assert not r.can_release_position_slot
    assert not r.can_submit_exchange_stop_amendment
    assert not r.can_admit_new_position
    assert not r.live_authorization and r.mexc_posts==0
    assert r.database_writes==0


def test_plus_five_has_no_fixed_take_profit_and_keeps_sl75():
    cps=(checkpoint(0),)
    r=project(cps)
    assert r.status=="V40_PORTFOLIO_VERIFIED_STOP_MODEL_UNCHANGED_NONAUTHORIZING"
    assert r.slots[0].decision.proposed_checkpoint.best_profit_pct==D("5")
    assert r.slots[0].decision.proposed_checkpoint.native_stop_price==D("175")
    assert not r.any_unresolved_venue_transition
    assert not r.can_admit_new_position


def test_four_simultaneously_armed_exits_are_only_proposals():
    cps=tuple(checkpoint(i) for i in range(4))
    bars={c.symbol:candle(low="86",close="86") for c in cps}
    r=project(cps,bars)
    assert r.proposed_stop_amendment_count==4
    assert r.native_close_reconciliation_count==0
    assert all(s.decision.proposed_native_stop_price==D("86.50") for s in r.slots)
    assert not r.can_submit_exchange_stop_amendment
    assert not r.live_authorization


def test_adverse_first_wins_favorable_low_in_single_bar():
    cps=(checkpoint(0),)
    r=project(cps,{cps[0].symbol:candle(high="190",low="70",close="140",open_="185")})
    assert r.slots[0].requires_native_order_reconciliation
    assert r.slots[0].decision.model_exit_price==D("185")
    assert r.slots[0].proposed_stop_price is None


def test_gap_pre_entry_incomplete_one_slot_invalidates_full_batch():
    cps=tuple(checkpoint(i) for i in range(4))
    for bad in (
        candle(ts=T+1800),candle(ts=T-1800),
        candle(high="90",low="95",close="96"),
    ):
        bars={c.symbol:candle() for c in cps}
        bars[cps[-1].symbol]=bad
        with pytest.raises(V40PortfolioExitBatchHalted):
            project(cps,bars)
    with pytest.raises(V40PortfolioExitBatchHalted):
        project(cps,now=T+900)


def test_missing_or_unknown_symbol_is_not_ignored():
    cps=tuple(checkpoint(i) for i in range(4))
    bars={c.symbol:candle() for c in cps}
    del bars[cps[0].symbol]
    with pytest.raises(V40PortfolioExitBatchHalted,match="COVERAGE"):
        project(cps,bars)
    bars[cps[0].symbol]=candle()
    bars["UNRELATED_USDT"]=candle()
    with pytest.raises(V40PortfolioExitBatchHalted,match="COVERAGE"):
        project(cps,bars)


@pytest.mark.parametrize("changes",[
    {"checkpoints":[]},
    {"checkpoints":[checkpoint(0)]*5},
    {"checkpoints":(checkpoint(0),checkpoint(0))},
    {"checkpoints":(checkpoint(0),replace(checkpoint(1),position_id="100"))},
    {"checkpoints":(checkpoint(0),replace(checkpoint(1),symbol="ALT0_USDT"))},
    {"checkpoints":(object(),),"bars":{},"ticks":{}},
    {"now":False},
])
def test_invalid_slot_identity_or_count_rejected(changes):
    with pytest.raises(V40PortfolioExitBatchHalted):
        project(**changes)


def test_invalid_tick_and_unmatched_bar_reject_without_partial_result():
    cps=tuple(checkpoint(i) for i in range(4))
    for bad in (D("0"),D("-0.01"),D("NaN"),"0.01",None):
        ticks={c.symbol:D("0.01") for c in cps}
        ticks[cps[3].symbol]=bad
        with pytest.raises(V40PortfolioExitBatchHalted):
            project(cps,ticks=ticks)
    bars={c.symbol:candle() for c in cps}
    bars[cps[3].symbol]=None
    with pytest.raises(V40PortfolioExitBatchHalted):
        project(cps,bars)
