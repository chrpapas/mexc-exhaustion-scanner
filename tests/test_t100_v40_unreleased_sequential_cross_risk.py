"""Historical V40 sequential lot plans vs exchange fair-price stress are not permits."""
from dataclasses import replace
from decimal import Decimal as D

import pytest

from app.t100_live_cross_margin_stress import CrossAccount, ShortCrossPosition
from app.t100_v40_unreleased_sequential_cross_risk import (
    V40CrossRiskBridgeHalted,compare_v40_sequential_lots_with_cross_stress,
)
from tests.test_t100_v40_unreleased_sequential_lot_margin import (
    projection,audit,
)


def evidence(atr=5.8):
    seq=audit(projection(atr=atr),leverage=5)
    assert seq.lot_plans
    return seq


def venue_rows(seq):
    return tuple(ShortCrossPosition(
        symbol=s.symbol,entry_fair_price=D("100"),current_fair_price=D("100"),
        contracts=s.plan.volume_contracts,contract_size=D("0.001"),
        configured_leverage=5,
        tier_max_position_notional_usdt=D("100000"),
        worst_stress_mmr=D("0.05"),
        liquidation_fee_rate=D("0.01"),
        stressed_tier_upper_bound_verified=True,
        cross_margin_verified=True,fair_price_verified=True,
    ) for s in seq.lot_plans)


def account(**kw):
    d=dict(
        wallet_balance_usdt=D(2000),
        isolated_position_margin_usdt=D(0),pending_order_margin_usdt=D(0),
        committed_cash_reserve_usdt=D(0),
        entry_and_future_fees_reserve_usdt=D(0),
        additional_adverse_cashflow_usdt=D(0),
        account_inventory_complete_verified=True,
        usdt_wallet_economic_witness_verified=True,
        risk_limits_fresh_verified=True,
    )
    d.update(kw)
    return CrossAccount(**d)


def compare(seq,rows=None,acct=None):
    return compare_v40_sequential_lots_with_cross_stress(
        sequential=seq,positions=rows if rows is not None else venue_rows(seq),
        account=acct if acct is not None else account(),
    )


def test_frozen_four_high_can_pass_initial_and_20pct_but_fail_sl75():
    seq=evidence()
    r=compare(seq)
    assert r.all_symbols_and_lots_reconciled
    assert r.expected_portfolio_notional_usdt==r.independently_provided_portfolio_entry_notional_usdt
    assert len(seq.lot_plans)==4
    assert r.shock20.modeled_stress_survived
    assert not r.stop75.modeled_stress_survived
    assert not r.stop75_gap5.modeled_stress_survived
    assert r.status=="V40_CROSS_RISK_ONE_OR_MORE_SHOCKS_FAIL_NONAUTHORIZING"
    assert not r.exchange_liquidation_certified
    assert not r.native_stop_execution_certified
    assert not r.live_strategy_equivalence_certified
    assert not r.entry_allowed and not r.live_authorization
    assert r.mexc_posts==0 and r.database_writes==0


def test_no_stressed_tier_bound_is_not_assumed_to_be_five_percent():
    seq=evidence()
    rows=venue_rows(seq)
    rows=(replace(rows[0],stressed_tier_upper_bound_verified=False),)+rows[1:]
    r=compare(seq,rows)
    assert r.status=="V40_CROSS_RISK_UNVERIFIED_ACCOUNT_OR_TIER_HALT"
    assert r.stop75.status=="CROSS_STRESS_EXCHANGE_EVIDENCE_INCOMPLETE"
    assert not r.all_scenarios_survive_model
    assert not r.live_authorization


def test_unverified_wallet_inventory_must_halt():
    seq=evidence()
    r=compare(seq,acct=account(account_inventory_complete_verified=False))
    assert r.status=="V40_CROSS_RISK_UNVERIFIED_ACCOUNT_OR_TIER_HALT"


def test_unrealistic_safe_model_still_never_certifies_real_liquidation():
    seq=evidence(atr=4.5)
    r=compare(seq,acct=account(wallet_balance_usdt=D("1000000")))
    assert r.all_scenarios_survive_model
    assert r.status=="V40_CROSS_RISK_THREE_SHOCK_MODELS_SURVIVE_NONAUTHORIZING"
    assert not r.exchange_liquidation_certified
    assert not r.live_authorization


@pytest.mark.parametrize("which",["qty","leverage","symbol","duplicate","fair_price"])
def test_contract_or_exchange_identity_drifts_are_hard_halts(which):
    seq=evidence()
    rows=list(venue_rows(seq))
    if which=="qty":
        rows[0]=replace(rows[0],contracts=rows[0].contracts+D(1))
    if which=="leverage":
        rows[0]=replace(rows[0],configured_leverage=1)
    if which=="symbol":
        rows[0]=replace(rows[0],symbol="OTHER_USDT")
    if which=="duplicate":
        rows[0]=replace(rows[0],symbol=rows[1].symbol)
    if which=="fair_price":
        rows[0]=replace(rows[0],entry_fair_price=D("99.0"))
    with pytest.raises(V40CrossRiskBridgeHalted):
        compare(seq,tuple(rows))


def test_no_live_margin_permit_can_be_forged_by_reference_boolean():
    seq=evidence()
    forged=replace(seq,live_authorization=True)
    with pytest.raises(V40CrossRiskBridgeHalted,match="SOURCE_EVIDENCE"):
        compare(forged)
