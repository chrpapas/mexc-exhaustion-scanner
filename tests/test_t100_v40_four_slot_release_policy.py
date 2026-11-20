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
from app.t100_v40_exchange_leverage_plan import (
    V40CrossLeverageInputs,assess_exact_v40_cross_leverage,
)


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
        exchange_leverage_plan=assess_exact_v40_cross_leverage(
            V40CrossLeverageInputs(
                signal_id=497,symbol="SIREN_USDT",
                frozen_lot_notional_usdt=D("60"),
                executable_intent_notional_usdt=D("60"),
                current_gross_notional_usdt=D("70"),
                account_equity_usdt=D("75"),
                signed_available_initial_margin_usdt=D("60"),
                independent_fee_reserve_usdt=D("1"),
                required_remaining_margin_headroom_usdt=D("10"),
                observed_contract_max_cross_leverage=5,
                observed_account_max_cross_leverage=10,
                operator_max_cross_leverage=5,
                account_cross_mode_verified=True,
                account_identity_bookended_verified=True,
                available_margin_private_signed_get_verified=True,
                per_contract_and_private_leverage_tiers_verified=True,
                frozen_strategy_lot_and_signal_verified=True,
            )
        ),
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
    canary_controls=controls(
        leverage="1",max_order="12.50",max_total="12.50",max_loss="12.50",
    )
    good={f.name:True for f in fields(policy.V40FirstCanaryReadinessEvidence)}
    tiny=policy.issue_v40_supervised_canary_permit(
        canary_controls,
        policy.V40FirstCanaryReadinessEvidence(**good),
        signal_id=497,symbol="SIREN_USDT",
    )
    assert tiny.scope==policy.V40_CANARY_SCOPE
    assert tiny.scope!=policy.V40_PORTFOLIO_SCOPE
    assert tiny.production_trial_accepted is True
    assert tiny.leverage==1 and tiny.max_total_notional_usdt==D("12.50")


def test_positive_margin_numbers_but_no_independent_leverage_plan_always_block():
    with pytest.raises(
        policy.LiveAuthorizationHalted,
        match="exact_frozen_notional_exchange_leverage_unverified",
    ):
        issue(e=evidence(exchange_leverage_plan=None))


def test_portfolio_controls_can_select_3x_from_separate_profile_only():
    from app.t100_live_guards import T100ExecutionControls
    basic={
        "T100_EXECUTION_MODE":"live",
        "T100_LIVE_ORDER_API_ENABLED":"true",
        "T100_LIVE_KILL_SWITCH":"false",
        "T100_LIVE_OPERATOR_CONFIRM":LIVE_CONFIRMATION,
        "T100_LIVE_MAX_POSITION_NOTIONAL_USDT":"5000",
        "T100_LIVE_MAX_TOTAL_NOTIONAL_USDT":"5000",
        "T100_LIVE_MAX_DAILY_LOSS_USDT":"500",
        "T100_LIVE_LEVERAGE":"1",
        "T100_V40_PORTFOLIO_EXCHANGE_LEVERAGE":"3",
    }
    portfolio=T100ExecutionControls.from_v40_portfolio_env(basic)
    assert portfolio.leverage==3
    assert T100ExecutionControls.from_env(basic).leverage==1
    with pytest.raises(ValueError,match="between 1 and 2"):
        T100ExecutionControls.from_env({
            **basic,"T100_LIVE_LEVERAGE":"3",
        })
    with pytest.raises(ValueError,match="explicitly set"):
        T100ExecutionControls.from_v40_portfolio_env({
            k:v for k,v in basic.items()
            if k!="T100_V40_PORTFOLIO_EXCHANGE_LEVERAGE"
        })
    with pytest.raises(ValueError,match="between 1 and 5"):
        T100ExecutionControls.from_v40_portfolio_env({
            **basic,"T100_V40_PORTFOLIO_EXCHANGE_LEVERAGE":"25",
        })


def test_3x_portfolio_permit_exact_notional_without_rewriting_historical_size():
    from app.t100_live_guards import T100ExecutionControls
    values={
        "T100_EXECUTION_MODE":"live",
        "T100_LIVE_ORDER_API_ENABLED":"true",
        "T100_LIVE_KILL_SWITCH":"false",
        "T100_LIVE_OPERATOR_CONFIRM":LIVE_CONFIRMATION,
        "T100_LIVE_MAX_POSITION_NOTIONAL_USDT":"4180",
        "T100_LIVE_MAX_TOTAL_NOTIONAL_USDT":"5000",
        "T100_LIVE_MAX_DAILY_LOSS_USDT":"1000",
        "T100_V40_PORTFOLIO_EXCHANGE_LEVERAGE":"3",
    }
    c=T100ExecutionControls.from_v40_portfolio_env(values)
    p=assess_exact_v40_cross_leverage(V40CrossLeverageInputs(
        signal_id=497,symbol="SIREN_USDT",
        frozen_lot_notional_usdt=D("4180"),
        executable_intent_notional_usdt=D("4180"),
        current_gross_notional_usdt=D("0"),
        account_equity_usdt=D("2000"),
        signed_available_initial_margin_usdt=D("2000"),
        independent_fee_reserve_usdt=D("20"),
        required_remaining_margin_headroom_usdt=D("100"),
        observed_contract_max_cross_leverage=5,
        observed_account_max_cross_leverage=10,
        operator_max_cross_leverage=5,
        account_cross_mode_verified=True,
        account_identity_bookended_verified=True,
        available_margin_private_signed_get_verified=True,
        per_contract_and_private_leverage_tiers_verified=True,
        frozen_strategy_lot_and_signal_verified=True,
    ))
    e=evidence(
        observed_equity_usdt=D("2000"),
        projected_gross_notional_after_entry_usdt=D("4180"),
        exchange_leverage_plan=p,
    )
    a,b,d=release_gate()
    with a,b,d:
        permit=issue(c=c,e=e)
        assert permit.leverage==3
        assert permit.max_total_notional_usdt==D("5000")
        assert p.exact_order_notional_usdt==D("4180")
        assert policy.require_certified_transport_permit(permit) is permit
    with pytest.raises(policy.LiveAuthorizationHalted,match="PRODUCTION_RELEASE_NOT_CERTIFIED"):
        policy.require_certified_transport_permit(permit)
