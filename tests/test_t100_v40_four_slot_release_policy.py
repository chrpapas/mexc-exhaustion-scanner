"""Portfolio-scoped V40 live permit policy (offline, no credentials or POST).

Proof fields are independently verified prerequisites, NOT env toggles.
The production release constant stays NOT_CERTIFIED; test overrides are
scoped to these unit tests and do not promote any deployed runtime.
"""
from dataclasses import fields,replace
from decimal import Decimal as D
from unittest.mock import patch
import pytest

from app.t100_live_guards import (
    T100ExecutionControls,LIVE_CONFIRMATION,
)
from app import t100_live_authorization as policy


def controls(*, leverage="2",max_order="60",max_total="140",max_loss="40"):
    return T100ExecutionControls.from_env({
        "T100_EXECUTION_MODE":"live",
        "T100_LIVE_ORDER_API_ENABLED":"true",
        "T100_LIVE_KILL_SWITCH":"false",
        "T100_LIVE_OPERATOR_CONFIRM":LIVE_CONFIRMATION,
        "T100_LIVE_MAX_POSITION_NOTIONAL_USDT":max_order,
        "T100_LIVE_MAX_TOTAL_NOTIONAL_USDT":max_total,
        "T100_LIVE_MAX_DAILY_LOSS_USDT":max_loss,
        "T100_LIVE_LEVERAGE":leverage,
    })


def evidence(**overrides):
    facts={x.name:True for x in fields(policy.V40FourSlotReleaseEvidence)}
    facts.update(
        observed_equity_usdt=D("75"),
        projected_gross_notional_after_entry_usdt=D("130"),
        projected_open_position_count_after_entry=3,
        modeled_sl75_stress_collateral_surplus_usdt=D("10"),
    )
    facts.update(overrides)
    return policy.V40FourSlotReleaseEvidence(**facts)


def release_gate():
    return (
        patch.object(policy,"V40_PORTFOLIO_RELEASE_STATUS","CERTIFIED_LIVE"),
        patch("app.t100_live_guards.LIVE_ADAPTER_STATUS","CERTIFIED"),
        patch("app.t100_live_guards.LIVE_NATIVE_STOP_AMENDMENT_STATUS","CERTIFIED"),
    )


def issue(c=None,e=None,signal_id=497,symbol="SIREN_USDT"):
    return policy.issue_v40_four_slot_portfolio_permit(
        c or controls(), e or evidence(),
        signal_id=signal_id,symbol=symbol,
    )


def test_normal_production_release_stays_closed_even_with_all_fake_positive_proofs():
    assert policy.V40_PORTFOLIO_RELEASE_STATUS=="NOT_CERTIFIED"
    with pytest.raises(policy.LiveAuthorizationHalted,match="SEPARATE_PRODUCTION_RELEASE_REQUIRED"):
        issue()


@pytest.mark.parametrize("flag",[
    f.name for f in fields(policy.V40FourSlotReleaseEvidence)
    if f.type is bool or f.name.endswith("_verified")
])
def test_each_absent_requisite_proof_blocks_permit(flag):
    if flag in (
        "observed_equity_usdt",
        "projected_gross_notional_after_entry_usdt",
        "projected_open_position_count_after_entry",
        "modeled_sl75_stress_collateral_surplus_usdt",
    ):
        return
    with pytest.raises(policy.LiveAuthorizationHalted,match=flag):
        issue(e=evidence(**{flag:False}))


@pytest.mark.parametrize("kwargs,reason",[
    ({"observed_equity_usdt":D("50")}, "cross_margin_gross_exceeds_leverage_capacity"),
    ({"observed_equity_usdt":D("0")}, "cross_margin_sl75_not_survivable"),
    ({"modeled_sl75_stress_collateral_surplus_usdt":D("-1")}, "cross_margin_sl75_not_survivable"),
    ({"modeled_sl75_stress_collateral_surplus_usdt":D("NaN")}, "cross_margin_numbers_invalid"),
    ({"projected_open_position_count_after_entry":5}, "four_slot_occupancy_invalid"),
    ({"projected_open_position_count_after_entry":0}, "four_slot_occupancy_invalid"),
])
def test_real_world_margin_and_portfolio_envelope_are_independent_blockers(kwargs,reason):
    with pytest.raises(policy.LiveAuthorizationHalted,match=reason):
        issue(e=evidence(**kwargs))


def test_exact_250pct_gross_requires_real_exchange_capacity_not_frozen_5x_assumption():
    # $56 pilot capital, 250% historical gross = $140. Even under the
    # existing maximum 2x cross leverage this is unachievable without
    # a margin/contract policy change; no silent sizing reduction.
    with pytest.raises(
        policy.LiveAuthorizationHalted,
        match="cross_margin_gross_exceeds_leverage_capacity",
    ):
        issue(c=controls(max_total="140"),e=evidence(
            observed_equity_usdt=D("56"),
            projected_gross_notional_after_entry_usdt=D("140"),
        ))


def test_caps_below_frozen_gross_cannot_replace_full_strategy():
    with pytest.raises(policy.LiveAuthorizationHalted,match="portfolio_cap_below_exact_strategy_gross"):
        issue(c=controls(max_total="100",max_order="60"))


def test_daily_loss_cap_must_not_exceed_collateral():
    with pytest.raises(policy.LiveAuthorizationHalted,match="daily_loss_cap_exceeds_collateral"):
        issue(c=controls(max_loss="100"))


@pytest.mark.parametrize("symbol,signal_id",[
    ("BTC_USDT",497),("ETH_USDT",497),("BAD/USDT",497),
    ("SIREN_USDT",0),("SIREN_USDT",True),
])
def test_exact_signal_bound_id_invariant(symbol,signal_id):
    with pytest.raises(policy.LiveAuthorizationHalted,match="exact_live_candidate_invalid"):
        issue(symbol=symbol,signal_id=signal_id)


def test_portfolio_release_cannot_reuse_only_canary_certification():
    with patch.object(policy,"V40_PORTFOLIO_RELEASE_STATUS","CERTIFIED_LIVE"),\
         patch("app.t100_live_guards.LIVE_ADAPTER_STATUS","NOT_IMPLEMENTED"),\
         patch("app.t100_live_guards.LIVE_NATIVE_STOP_AMENDMENT_STATUS","UNVERIFIED_CURRENT_DOCS"):
        with pytest.raises(policy.LiveAuthorizationHalted,match="TRANSPORT_AND_STOP_RELEASE_REQUIRED"):
            issue()


def test_only_all_three_separately_reviewed_release_switches_can_issue():
    a,b,c=release_gate()
    with a,b,c:
        p=issue()
        assert type(p) is policy.LiveMutationPermit
        assert p.scope==policy.V40_PORTFOLIO_SCOPE
        assert p.production_trial_accepted is False
        assert p.signal_id==497 and p.symbol=="SIREN_USDT"
        assert p.leverage==2
        assert p.max_total_notional_usdt==D("140")
        assert policy.require_certified_transport_permit(p) is p
    with pytest.raises(
        policy.LiveAuthorizationHalted,match="PRODUCTION_RELEASE_NOT_CERTIFIED",
    ):
        policy.require_certified_transport_permit(p)


def test_legacy_and_canary_scopes_stay_unchanged():
    with patch.object(policy,"V40_PORTFOLIO_RELEASE_STATUS","CERTIFIED_LIVE"):
        with pytest.raises(policy.LiveAuthorizationHalted,match="V40_PORTFOLIO_TRANSPORT"):
            # Existing tiny canary evidence must not become a portfolio permit.
            fake=object()
            policy.require_certified_transport_permit(fake)
