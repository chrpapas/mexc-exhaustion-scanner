"""Final V40 real POST context must match newly priced 1-3-owned signal.

Never stage, POST, grant a fake permit, touch production DB, or assume
fixed leverage. All assertions are pure model identity checks.
"""
from dataclasses import replace
from decimal import Decimal as D

import pytest

from app.t100_live_v40_entry_coordinator import (
    V40PreparedEntry,verify_fresh_exact_depth,
    verified_exact_portfolio_post_context,
)
from app.t100_v40_portfolio_order_transport import V40PortfolioPostContext
from tests.test_t100_v40_owned_cross_candidate_witness import setup


async def assembled(n=2):
    f=setup(n)
    risk,terms=await f.w.inspect(f.signal)
    quote,depth=await f.w.quote_exact_intent(
        f.signal,f.intent,max_slippage_bps=f.caps.max_slippage_bps,
    )
    sized=verify_fresh_exact_depth(
        signal=f.signal,intent=f.intent,
        risk=risk,terms=terms,caps=f.caps,
        quote=quote,depth=depth,now_ms=f.now,
    )
    context=V40PortfolioPostContext(
        expected_occupied_slots=n,
        expected_owned_position_ids=tuple(str(i+1) for i in range(n)),
        frozen_order_notional_usdt=f.intent.notional_usdt,
        frozen_portfolio_gross_after_entry_usdt=(
            sized.portfolio_notional_usdt+f.intent.notional_usdt
        ),
        frozen_equity_at_plan_usdt=sized.current_account_equity_usdt,
        protected_remaining_margin_headroom_usdt=D("2.5"),
        fee_reserve_usdt=D("0.2"),
        leverage_plan=quote.exact_cross_leverage_plan,
    )
    selected=V40PreparedEntry(f.signal,None,f.intent,terms)
    return f,selected,sized,context


@pytest.mark.asyncio
@pytest.mark.parametrize("n",[1,2,3])
async def test_frozen_exact_lot_and_newly_observed_cross_plan_cannot_drift(n):
    f,selected,sized,context=await assembled(n)
    assert sized.cross_leverage_plan is context.leverage_plan
    assert sized.cross_leverage_plan.selected_exchange_cross_leverage==2
    assert verified_exact_portfolio_post_context(
        snapshot=f.state.snapshot,selected=selected,
        sized_risk=sized,caps=f.caps,context=context,
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("drift",[
    "signal","symbol","context_plan","size","equity","gross",
    "owned_count","owned_ids","risk_equity","risk_slots","margin",
    "fee","headroom","leverage","foreign_quote","missing_quote",
])
async def test_stale_post_adapter_or_wrong_lot_never_passes_last_fence(drift):
    f,selected,sized,context=await assembled()
    snap=f.state.snapshot
    caps=f.caps
    if drift=="signal":
        selected=replace(selected,signal=replace(f.signal,signal_id=999))
    elif drift=="symbol":
        selected=replace(selected,signal=replace(f.signal,symbol="WRONG_USDT"))
    elif drift=="context_plan":
        context=replace(context,leverage_plan=replace(
            context.leverage_plan,signal_id=999,
        ))
    elif drift=="size":
        context=replace(context,frozen_order_notional_usdt=D("20"))
    elif drift=="equity":
        context=replace(context,frozen_equity_at_plan_usdt=D("49"))
    elif drift=="gross":
        context=replace(context,frozen_portfolio_gross_after_entry_usdt=D("39"))
    elif drift=="owned_count":
        context=replace(context,expected_occupied_slots=3)
    elif drift=="owned_ids":
        context=replace(context,expected_owned_position_ids=("1","789"))
    elif drift=="risk_equity":
        sized=replace(sized,current_account_equity_usdt=D("49"))
    elif drift=="risk_slots":
        sized=replace(sized,current_owned_positions=1)
    elif drift=="margin":
        sized=replace(sized,available_margin_usdt=D("6"))
    elif drift=="fee":
        context=replace(context,fee_reserve_usdt=D("0"))
    elif drift=="headroom":
        context=replace(context,protected_remaining_margin_headroom_usdt=D("3"))
    elif drift=="leverage":
        caps=replace(caps,leverage=3)
    elif drift=="foreign_quote":
        sized=replace(sized,cross_leverage_plan=replace(
            sized.cross_leverage_plan,symbol="FOREIGN_USDT",
        ))
    elif drift=="missing_quote":
        sized=replace(sized,cross_leverage_plan=None)
    assert not verified_exact_portfolio_post_context(
        snapshot=snap,selected=selected,sized_risk=sized,
        caps=caps,context=context,
    )


def test_nonexistent_portfolio_context_never_turns_into_late_post():
    assert not verified_exact_portfolio_post_context(
        snapshot=None,selected=None,sized_risk=None,
        caps=None,context=None,
    )
