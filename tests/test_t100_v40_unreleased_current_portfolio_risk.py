"""Cross-observer risk identity: all 1–4 owned native SLs, published tiers.

All inputs synthetic; test does not contact MEXC or write to Postgres.
"""
from copy import deepcopy
from dataclasses import replace
from decimal import Decimal as D

import pytest

from app.t100_v40_unreleased_current_portfolio_risk import (
    audit_current_four_slot_risk,
)
from app.t100_live_cross_risk_observation import (
    CrossRiskObservation,CrossRiskRead,_static_hash,
)
from tests.test_t100_v40_four_slot_guardian_policy import evidence
from tests.test_t100_live_risk_tier_witness import private,public


def bundle(n=4):
    snap,ledger,positions,stops=evidence(n)
    ts=1_001_000
    risks={}
    contracts={}
    fair={}
    for pos in positions:
        sym=pos["symbol"]
        private_rows=private()["TEST_USDT"]
        private_rows[0]["symbol"]=sym
        risks[sym]=private_rows
        meta=public()
        meta["symbol"]=sym
        contracts[sym]=meta
        fair[sym]=dict(fairPrice="100",observed_at_ms=ts)
    read=CrossRiskRead(
        observed_at_ms=ts,
        positions=deepcopy(positions),
        stops=deepcopy(stops),
        assets=[dict(
            currency="USDT",equity="10000",cashBalance="10000",
            availableBalance="0",frozenBalance="0",
            positionMargin="0",unrealized="0",bonus="0",
            debtAmount="0",contributeMarginAmount="0",
        )],
        private_risk_by_symbol=risks,
        public_contract_by_symbol=contracts,
        fair_by_symbol=fair,
        positions_complete=True,stops_complete=True,assets_complete=True,
    )
    obs=CrossRiskObservation(
        status="CURRENT_CROSS_RISK_OBSERVED_NONAUTHORIZING",
        observed_positions=n,
        observed_current_stops=n,
        native_stop_protection_verified=False,
        stable_critical_inventory=True,
        position_margin_economic_witness_observed=True,
        current_risk_tiers_observed=True,
        fair_prices_observed=True,
        evidence_fingerprint_sha256=_static_hash(read),
    )
    return dict(
        snapshot=snap,ledger=ledger,risk_observation=obs,
        risk_read=read,now_ms=ts+1000,
    )


@pytest.mark.parametrize("n",[1,2,3,4])
def test_same_account_native_stop_and_current_private_risk_join(n):
    kw=bundle(n)
    result=audit_current_four_slot_risk(**kw)
    assert result.status=="V40_CURRENT_EXACT_FOUR_SLOT_FAIR_AND_TIER_RESEARCH_NONAUTHORIZING"
    assert result.slots==n
    assert result.observed_entry_notional_usdt==D("4000")*n
    assert result.current_gross_fair_notional_usdt==D("4000")*n
    assert result.all_owned_positions_and_native_stops_verified
    assert result.same_venue_inventory_in_both_readers
    assert result.current_private_public_tiers_verified
    assert len(result.shocked)==3
    assert [x.adverse_price_pct for x in result.shocked]==[
        D(".20"),D(".75"),D(".80"),
    ]
    gap=result.shocked[-1]
    assert gap.modeled_unrealized_short_pnl_usdt==-D("3200")*n
    assert gap.modeled_gross_fair_notional_usdt==D("7200")*n
    assert gap.current_published_mmr_reserve_usdt==D("576")*n
    assert gap.current_published_liquidation_fee_reserve_usdt==D("36")*n
    assert not gap.exchange_liquidation_survival_certified
    assert not result.future_tier_ceiling_verified
    assert not result.cross_wallet_collateral_verified
    assert not result.liquidation_before_sl75_excluded
    assert not result.native_sl75_execution_certified
    assert not result.historical_strategy_parity_certified
    assert not result.entry_allowed and not result.live_authorization
    assert result.mexc_posts==result.database_writes==0


@pytest.mark.parametrize("tamper",[
    "wrong_venue_stop","lost_native_stop","extra_position",
    "position_volume","position_leverage","private_leverage",
    "fair_timestamp","stale_read","missing_private_symbol",
    "wrong_risk_fingerprint","drifted_ledger","not_proven_native",
    "not_observed_full_account","negative_equity","missing_assets",
    "isolated_position","risk_read_incomplete",
])
def test_tampering_or_missing_economics_never_grants_four_slot_risk(tamper):
    x=bundle(4)
    if tamper=="wrong_venue_stop":
        x["risk_read"].stops[0]["stopLossPrice"]="176"
    if tamper=="lost_native_stop":
        x["risk_read"].stops.pop()
    if tamper=="extra_position":
        x["risk_read"].positions.append(
            dict(x["risk_read"].positions[0],positionId="900"))
    if tamper=="position_volume":
        x["risk_read"].positions[0]["holdVol"]="17"
    if tamper=="position_leverage":
        x["risk_read"].positions[0]["leverage"]=1
    if tamper=="private_leverage":
        sym=x["risk_read"].positions[0]["symbol"]
        x["risk_read"].private_risk_by_symbol[sym][0]["leverage"]=1
    if tamper=="fair_timestamp":
        sym=x["risk_read"].positions[0]["symbol"]
        x["risk_read"].fair_by_symbol[sym]["observed_at_ms"]=900
    if tamper=="stale_read":
        x["now_ms"]+=6000
    if tamper=="missing_private_symbol":
        x["risk_read"].private_risk_by_symbol.pop(
            next(iter(x["risk_read"].private_risk_by_symbol))
        )
    if tamper=="wrong_risk_fingerprint":
        x["risk_observation"]=replace(x["risk_observation"],
                                     evidence_fingerprint_sha256="0"*64)
    if tamper=="drifted_ledger":
        x["ledger"].checkpoints[0]["stop_price"]=D("174")
    if tamper=="not_proven_native":
        x["risk_observation"]=replace(
            x["risk_observation"],native_stop_protection_verified=True)
    if tamper=="not_observed_full_account":
        x["risk_observation"]=replace(
            x["risk_observation"],account_wide_completeness_certified=True)
    if tamper=="negative_equity":
        x["risk_read"].assets[0]["equity"]="-1"
    if tamper=="missing_assets":
        x["risk_read"]=replace(x["risk_read"],assets=[])
    if tamper=="isolated_position":
        x["risk_read"].positions[0]["openType"]=1
    if tamper=="risk_read_incomplete":
        x["risk_read"]=replace(x["risk_read"],assets_complete=False)
    # An attacker cannot hide a changed risk/contract/account field by
    # merely recalculating the read fingerprint: exact ledger/stop/account
    # or independent tier validation must still fail when appropriate.
    if tamper in ("position_leverage","private_leverage","fair_timestamp",
                  "negative_equity","missing_assets","missing_private_symbol"):
        x["risk_observation"]=replace(
            x["risk_observation"],
            evidence_fingerprint_sha256=_static_hash(x["risk_read"]),
        )
    out=audit_current_four_slot_risk(**x)
    assert out.status!="V40_CURRENT_EXACT_FOUR_SLOT_FAIR_AND_TIER_RESEARCH_NONAUTHORIZING"
    assert not out.all_owned_positions_and_native_stops_verified
    assert not out.cross_wallet_collateral_verified
    assert not out.future_tier_ceiling_verified
    assert not out.entry_allowed and not out.live_authorization


def test_a_good_published_80pct_stress_is_not_exchange_liquidation_proof():
    x=bundle(1)
    out=audit_current_four_slot_risk(**x)
    assert out.shocked[-1].current_published_mmr_reserve_usdt>0
    assert out.observed_account_equity_usdt>D("0")
    assert out.liquidation_before_sl75_excluded is False
    assert out.cross_wallet_collateral_verified is False
