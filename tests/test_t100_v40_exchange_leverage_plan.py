"""V40 original LOT sizing versus exchange-selected CROSS leverage (offline).

No MEXC account requests, POST, leverage mutation, missing stop or cash risk
claims. The planner cannot certify liquidation or release live orders.
"""
from dataclasses import replace
from decimal import Decimal as D
import pytest

from app.t100_v40_exchange_leverage_plan import (
    V40CrossLeverageInputs,V40LeveragePlanHalted,
    STATUS_FEASIBLE,assess_exact_v40_cross_leverage,
)


def witness(**kwargs):
    values=dict(
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
    )
    values.update(kwargs)
    return V40CrossLeverageInputs(**values)


def test_209pct_gross_requires_3x_for_nominal_initial_margin_but_does_not_resize():
    """4,180 notionals on 2,000 equity: <=2x insufficient; >=3x nominally fits."""
    r=assess_exact_v40_cross_leverage(witness())
    assert r.status==STATUS_FEASIBLE
    assert r.selected_exchange_cross_leverage==3
    assert r.exact_order_notional_usdt==D("4180")
    assert r.portfolio_gross_after_entry_usdt==D("4180")
    assert r.portfolio_effective_gross_multiple==D("2.09")
    assert r.required_initial_margin_usdt==D("4180")/D(3)
    assert r.nominal_initial_margin_fits
    assert r.frozen_strategy_notional_unchanged
    assert r.account_observations_verified
    assert not r.liquidation_or_stop_survival_certified
    assert not r.live_change_allowed and not r.live_authorization
    assert r.mexc_posts==r.database_writes==0


def test_250pct_theoretical_ceiling_is_not_position_size_multiplier():
    r=assess_exact_v40_cross_leverage(witness(
        frozen_lot_notional_usdt=D("5000"),
        executable_intent_notional_usdt=D("5000"),
    ))
    assert r.selected_exchange_cross_leverage==3
    assert r.exact_order_notional_usdt==D("5000")
    assert r.portfolio_effective_gross_multiple==D("2.5")
    assert r.liquidation_or_stop_survival_certified is False


@pytest.mark.parametrize("limits",[
    dict(observed_contract_max_cross_leverage=2),
    dict(observed_account_max_cross_leverage=2),
    dict(operator_max_cross_leverage=2),
])
def test_contract_account_or_operator_leverage_limit_blocks_full_historical_lot(limits):
    r=assess_exact_v40_cross_leverage(witness(**limits))
    assert r.status=="V40_EXACT_LOT_NO_SUPPORTED_CROSS_LEVERAGE_MARGIN_HALT"
    assert r.selected_exchange_cross_leverage is None
    assert not r.nominal_initial_margin_fits and not r.live_authorization
    assert r.exact_order_notional_usdt==D("4180")


def test_readily_available_margin_only_covers_incremental_new_position():
    # Other positions already consume their initial margin. The signed
    # remaining balance must not be charged for their margin a second time.
    r=assess_exact_v40_cross_leverage(witness(
        frozen_lot_notional_usdt=D("1000"),
        executable_intent_notional_usdt=D("1000"),
        current_gross_notional_usdt=D("3180"),
        signed_available_initial_margin_usdt=D("500"),
        independent_fee_reserve_usdt=D("5"),
        required_remaining_margin_headroom_usdt=D("100"),
    ))
    assert r.selected_exchange_cross_leverage==3
    assert r.portfolio_gross_after_entry_usdt==D("4180")
    assert r.portfolio_effective_gross_multiple==D("2.09")
    assert r.exact_order_notional_usdt==D("1000")


def test_less_exposure_uses_1x_and_never_forces_historical_5x_proxy():
    r=assess_exact_v40_cross_leverage(witness(
        frozen_lot_notional_usdt=D("250"),
        executable_intent_notional_usdt=D("250"),
        observed_contract_max_cross_leverage=50,
        operator_max_cross_leverage=5,
    ))
    assert r.selected_exchange_cross_leverage==1
    assert r.required_initial_margin_usdt==D("250")


@pytest.mark.parametrize("changed",[
    dict(account_cross_mode_verified=False),
    dict(account_identity_bookended_verified=False),
    dict(available_margin_private_signed_get_verified=False),
    dict(per_contract_and_private_leverage_tiers_verified=False),
    dict(frozen_strategy_lot_and_signal_verified=False),
])
def test_unverified_account_or_strategy_source_must_fail_before_leverage_selection(changed):
    with pytest.raises(
        V40LeveragePlanHalted,match="SIGNED_EXCHANGE_OR_LOT_PROOF_MISSING",
    ):
        assess_exact_v40_cross_leverage(witness(**changed))


def test_reject_shrinking_strategy_lot_to_make_leverage_fit():
    with pytest.raises(V40LeveragePlanHalted,match="CANNOT_RESIZE_HISTORICAL_LOT"):
        assess_exact_v40_cross_leverage(witness(
            executable_intent_notional_usdt=D("12.50"),
        ))


@pytest.mark.parametrize("bad",[
    dict(signal_id=0),dict(signal_id=True),dict(symbol="BTC_USDT"),
    dict(frozen_lot_notional_usdt=D("NaN")),
    dict(signed_available_initial_margin_usdt=D("0")),
    dict(independent_fee_reserve_usdt=D("-1")),
    dict(observed_contract_max_cross_leverage=0),
    dict(observed_account_max_cross_leverage=True),
    dict(operator_max_cross_leverage=126),
])
def test_untrusted_input_rejected_without_get_post_or_ledger_changes(bad):
    with pytest.raises(V40LeveragePlanHalted,match="SOURCE_OR_LIMITS_INVALID"):
        assess_exact_v40_cross_leverage(witness(**bad))


def test_high_required_cash_buffer_makes_nominal_margin_infeasible():
    r=assess_exact_v40_cross_leverage(witness(
        required_remaining_margin_headroom_usdt=D("1500"),
    ))
    assert r.status=="V40_EXACT_LOT_NO_SUPPORTED_CROSS_LEVERAGE_MARGIN_HALT"
    assert r.selected_exchange_cross_leverage is None


def test_negative_liquidation_proof_never_magically_changes_with_leverage():
    for lv in (1,2,3,4,5):
        r=assess_exact_v40_cross_leverage(witness(
            operator_max_cross_leverage=lv,
        ))
        assert r.liquidation_or_stop_survival_certified is False
        assert r.live_change_allowed is False
        assert r.mexc_posts==0
