"""Actual 1-3 protected account quote + exact frozen lot depth, no POST.

Unknown/foreign positions, collateral drift, invalid write scope, stale or
crossed depth, wrong market leverage, no full daily PnL all fail closed.
"""
from dataclasses import replace
from decimal import Decimal as D
from types import SimpleNamespace

import pytest

from app.t100_live_readonly_probe import ReadOnlyMexcProbe
from app.t100_live_execution import EntryIntent,LiveCaps
from app.t100_live_four_slot_snapshot_coordinator import FourSlotSnapshotResult
from app.t100_live_v40_entry_coordinator import verify_fresh_exact_depth,V40CoordinatorHalted
from app.t100_v40_canary_key_permissions import V40KeyPermissionObservation
from app.t100_v40_canary_daily_loss import V40DailyLossEvidence
from app.t100_v40_exchange_leverage_plan import V40CrossLeveragePlan,STATUS_FEASIBLE
from app.t100_v40_unreleased_current_portfolio_risk import V40PortfolioCurrentRisk
from app import t100_v40_owned_cross_candidate_witness as m
from tests.test_t100_live_v40_entry_coordinator import candidate


class Result:
    def __init__(self,data):self.data=data
    def raise_for_status(self):return None
    def json(self):return {"success":True,"code":0,"data":self.data}


def setup(n=2):
    signal=candidate()
    now=signal.admission_open_ts*1000+900
    if now<=0:raise AssertionError("source fixture timestamp invalid")
    start_hash="a"*64
    end_hash="b"*64
    snapshot=FourSlotSnapshotResult(
        "NONFLAT_PROVENANCE_MATCHED_NONAUTHORIZING",
        occupied_slots=n,
        recovery=SimpleNamespace(recovered=tuple(
            SimpleNamespace(position_id=str(i+1),symbol=f"HELD{i}_USDT")
            for i in range(n)
        )),
        ledger_hash=start_hash,critical_inventory_hash=end_hash,
    )
    state=SimpleNamespace(
        snapshot=snapshot,
        equity="50",available="20",mode=1,
        write_key=True,complete_history=True,
        leverage=2,market_state=0,
        depth_ts=now-100,
        crossed=False,
        calls=[],
        risk_good=True,
    )
    async def inspect():
        state.calls.append("full_snapshot")
        return state.snapshot
    async def risk():
        state.calls.append("owned_same_signer_risk")
        return V40PortfolioCurrentRisk(
            status=("V40_CURRENT_EXACT_FOUR_SLOT_FAIR_AND_TIER_RESEARCH_NONAUTHORIZING"
                    if state.risk_good else "V40_RISK_BAD"),
            slots=n,observed_account_equity_usdt=D("50"),
            current_gross_fair_notional_usdt=D("30"),
            all_owned_positions_and_native_stops_verified=True,
            same_venue_inventory_in_both_readers=True,
            current_private_public_tiers_verified=True,
        )
    async def mode():return state.mode
    async def assets():
        state.calls.append("signed_assets")
        return [{
            "currency":"USDT","equity":state.equity,
            "availableBalance":state.available,
            "cashBalance":"50","frozenBalance":"0",
            "positionMargin":"30","unrealized":"0",
            "debtAmount":"0","contributeMarginAmount":"0","bonus":"0",
        }]
    async def leverage(symbol):
        assert symbol==signal.symbol
        return [{"positionType":2,"openType":2,"leverage":state.leverage}]
    async def risk_tier(symbol):
        assert symbol==signal.symbol
        return {symbol:[{
            "positionType":2,"symbol":symbol,"level":1,
            "maxVol":"1000","openType":2,"leverage":state.leverage,
        }]}
    class Public:
        async def get(self,path,*,params):
            state.calls.append("public_"+path)
            if path.startswith("/api/v1/contract/depth/"):
                return Result({
                    "timestamp":state.depth_ts,"version":1,
                    "bids":[["99.99","100"],["99.98","50"]],
                    "asks":[["100.01" if not state.crossed else "99.98","100"]],
                })
            if path=="/api/v1/contract/detail/country":
                return Result({
                    "symbol":signal.symbol,"apiAllowed":True,
                    "state":state.market_state,"quoteCoin":"USDT",
                    "settleCoin":"USDT","futureType":1,"positionOpenType":2,
                    "priceUnit":"0.01","volUnit":"1",
                    "minVol":"1","maxVol":"1000",
                    "contractSize":"0.01",
                })
            raise AssertionError("unsupported GET")
    public=Public()
    probe=object.__new__(ReadOnlyMexcProbe)
    probe.raw_position_mode=mode
    probe.raw_account_assets=assets
    probe.raw_position_leverage=leverage
    probe.raw_account_risk_limits=risk_tier
    async def key():
        state.calls.append("key_read")
        return V40KeyPermissionObservation(
            status=("V40_KEY_INFO_FUTURES_WRITE_SCOPE_OBSERVED_NONAUTHORIZING"
                    if state.write_key else "V40_KEY_INFO_FUTURES_WRITE_SCOPE_ABSENT"),
            api_key_identity_verified=True,
            api_key_active_verified=True,
            futures_account_read_scope_verified=True,
            futures_order_read_scope_verified=True,
            futures_order_write_scope_verified=state.write_key,
        )
    async def daily():
        state.calls.append("daily_history")
        return V40DailyLossEvidence(
            status=("V40_DAILY_LOSS_TWO_COMPLETE_SIGNED_GET_PASSES_NONAUTHORIZING"
                    if state.complete_history else "V40_DAILY_LOSS_HISTORY_TRUNCATED"),
            realized_today_loss_usdt="0.35",
            complete_two_passes=state.complete_history,
            complete_history_verified=state.complete_history,
        )
    w=object.__new__(m.V40OwnedCrossCandidateWitness)
    w.ledger=object()
    w.snapshots=SimpleNamespace(inspect=inspect)
    w.venue=object()
    w.private=probe
    w.public=public
    w.same_signer_risk=SimpleNamespace(observe_current=risk)
    w.read_key_permissions=key
    w.read_daily_loss=daily
    w.clock_ms=lambda:now
    w.max_book_age_ms=1500
    plan=V40CrossLeveragePlan(
        STATUS_FEASIBLE,signal_id=signal.signal_id,
        symbol=signal.symbol,
        exact_order_notional_usdt=D("10"),
        observed_equity_usdt=D("50"),
        selected_exchange_cross_leverage=2,
        required_initial_margin_usdt=D("5"),
        portfolio_gross_after_entry_usdt=D("40"),
        nominal_initial_margin_fits=True,
        frozen_strategy_notional_unchanged=True,
        account_observations_verified=True,
    )
    intent=EntryIntent(
        signal_id=signal.signal_id,symbol=signal.symbol,
        reference_price=D("100"),notional_usdt=D("10"),
        volume_contracts=D("10"),stop_price=D("175"),
        external_oid="test-owned-10",
    )
    caps=LiveCaps(
        max_order_usdt=D("100"),
        max_portfolio_usdt=D("400"),
        max_daily_loss_usdt=D("25"),
        leverage=2,risk_profile="T100_V40_FOUR_SLOT_PRODUCTION_TRIAL",
    )
    return SimpleNamespace(
        w=w,state=state,signal=signal,now=now,
        intent=intent,plan=plan,caps=caps,
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("n",[1,2,3])
async def test_signed_one_to_three_owned_stops_and_exact_sized_depth(n):
    f=setup(n)
    risk,terms=await f.w.inspect(f.signal)
    assert risk.current_owned_positions==n
    assert risk.realized_today_loss_usdt==D("0.35")
    assert risk.portfolio_notional_usdt==D("30")
    assert risk.observed_sell_vwap==D(0)
    assert risk.inventory_bookended_complete
    assert risk.no_unowned_positions_or_orders
    assert risk.cross_collateral_verified
    assert risk.hedge_mode_verified
    assert risk.private_risk_tier_verified
    assert risk.contract_trading_access_verified
    assert risk.no_unresolved_mutations
    assert terms.private_order_permission_verified
    assert f.state.calls.index("daily_history")<f.state.calls.index("full_snapshot")
    quote,depth=await f.w.quote_exact_intent(
        f.signal,f.intent,max_slippage_bps=D("100"),
    )
    assert quote.bookended_flat_account is False
    assert quote.owned_protected_positions_bookended==n
    assert quote.private_key_order_permission_verified is True
    assert quote.complete_realized_daily_loss_verified is True
    assert depth.contracts==D("10")
    assert depth.sell_vwap==D("99.99")
    risk=replace(risk,cross_leverage_plan=f.plan)
    sized=verify_fresh_exact_depth(
        signal=f.signal,intent=f.intent,
        risk=risk,terms=terms,caps=f.caps,
        quote=quote,depth=depth,now_ms=f.now,
    )
    assert sized.observed_sell_vwap==D("99.99")
    assert sized.current_owned_positions==n
    assert sized.cross_leverage_plan is f.plan
    assert sized.no_unowned_positions_or_orders
    assert sized.book_age_ms==100


@pytest.mark.asyncio
@pytest.mark.parametrize("fault",[
    "foreign_owned","four_slots","changed_inventory",
    "no_history","no_key","wrong_position_mode","wrong_leverage",
    "crossed_book","stale_depth","bad_current_risk","invalid_market",
    "changed_available","wrong_equity",
])
async def test_ambiguous_candidate_economics_blocks_before_any_dispatch(fault):
    f=setup()
    s=f.state
    if fault=="foreign_owned":
        s.snapshot=replace(s.snapshot,recovery=SimpleNamespace(
            recovered=(SimpleNamespace(symbol=f.signal.symbol,position_id="1"),),
        ))
    elif fault=="four_slots":
        s.snapshot=replace(s.snapshot,occupied_slots=4)
    elif fault=="changed_inventory":
        calls=0
        async def changing():
            nonlocal calls
            calls+=1
            return (s.snapshot if calls<2 else replace(
                s.snapshot,critical_inventory_hash="c"*64
            ))
        f.w.snapshots.inspect=changing
    elif fault=="no_history":
        s.complete_history=False
    elif fault=="no_key":
        s.write_key=False
    elif fault=="wrong_position_mode":
        s.mode=2
    elif fault=="wrong_leverage":
        s.leverage=9
    elif fault=="crossed_book":
        s.crossed=True
    elif fault=="stale_depth":
        s.depth_ts=f.now-999999
    elif fault=="bad_current_risk":
        s.risk_good=False
    elif fault=="invalid_market":
        s.market_state=2
    elif fault=="changed_available":
        calls=0
        async def changed_assets():
            nonlocal calls
            calls+=1
            rows=[{"currency":"USDT","equity":"50",
                   "availableBalance":"20" if calls==1 else "19",
                   "cashBalance":"50","frozenBalance":"0",
                   "positionMargin":"30","unrealized":"0",
                   "debtAmount":"0","contributeMarginAmount":"0","bonus":"0"}]
            return rows
        f.w.private.raw_account_assets=changed_assets
    elif fault=="wrong_equity":
        s.equity="40"
    with pytest.raises(m.V40OwnedCrossCandidateHalted):
        await f.w.inspect(f.signal)


@pytest.mark.asyncio
async def test_nominal_margin_plan_wrong_signal_or_insufficient_funds_rejected():
    f=setup()
    r,terms=await f.w.inspect(f.signal)
    q,depth=await f.w.quote_exact_intent(
        f.signal,f.intent,max_slippage_bps=D("100"),
    )
    for bad in (
        replace(f.plan,signal_id=999),
        replace(f.plan,required_initial_margin_usdt=D("50")),
        replace(f.plan,liquidation_or_stop_survival_certified=True),
    ):
        with pytest.raises(V40CoordinatorHalted):
            verify_fresh_exact_depth(
                signal=f.signal,intent=f.intent,
                risk=replace(r,cross_leverage_plan=bad),
                terms=terms,caps=f.caps,quote=q,
                depth=depth,now_ms=f.now,
            )


@pytest.mark.asyncio
async def test_flat_canary_quote_cannot_be_silently_treated_as_owned():
    f=setup()
    r,terms=await f.w.inspect(f.signal)
    q,depth=await f.w.quote_exact_intent(
        f.signal,f.intent,max_slippage_bps=D("100"),
    )
    with pytest.raises(V40CoordinatorHalted):
        verify_fresh_exact_depth(
            signal=f.signal,intent=f.intent,
            risk=replace(r,cross_leverage_plan=f.plan),
            terms=terms,caps=f.caps,
            quote=replace(q,bookended_flat_account=True),
            depth=depth,now_ms=f.now,
        )
