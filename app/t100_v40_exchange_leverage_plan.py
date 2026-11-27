"""V40 frozen notional -> MEXC selected CROSS leverage, without size changes.

The frozen V40 model chooses an executable contract-lot NOTIONAL. MEXC
selected leverage only changes initial-margin demand for that exact lot;
it MUST NEVER multiply the strategy's position notional or silently shrink
the strategy's quantity.

This strictly read-only arithmetic bridge is a necessary *nominal* account
margin check, not a liquidation/stressed-maintenance-tier certificate. The
inputs must be independently observed by signed account GET, contract/risk
limits and immutable V40 entry/lot witnesses. Never pass operator-supplied
or hardcoded "available margin" into an order coordinator as venue evidence.

MEXC documentation: position/leverage (GET); contract/detail/country (GET);
order/create with leverage and openType=2 (POST, NOT used here).
No MEXC POST, no SQL mutation, no account-setting change or live permit.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from decimal import Decimal as D, InvalidOperation, ROUND_CEILING


MAX_REVIEW_LEVERAGE=5
STATUS_FEASIBLE="V40_EXACT_LOT_CROSS_LEVERAGE_NOMINALLY_FEASIBLE_NONAUTHORIZING"


class V40LeveragePlanHalted(ValueError):
    """Fixed nonsecret failure code. Never include account data."""


@dataclass(frozen=True)
class V40CrossLeverageInputs:
    signal_id:int
    symbol:str
    frozen_lot_notional_usdt:D
    executable_intent_notional_usdt:D
    current_gross_notional_usdt:D
    account_equity_usdt:D
    signed_available_initial_margin_usdt:D
    independent_fee_reserve_usdt:D
    required_remaining_margin_headroom_usdt:D
    observed_contract_max_cross_leverage:int
    observed_account_max_cross_leverage:int
    operator_max_cross_leverage:int
    account_cross_mode_verified:bool
    account_identity_bookended_verified:bool
    available_margin_private_signed_get_verified:bool
    per_contract_and_private_leverage_tiers_verified:bool
    frozen_strategy_lot_and_signal_verified:bool


@dataclass(frozen=True)
class V40CrossLeveragePlan:
    status:str
    signal_id:int|None=None
    symbol:str|None=None
    exact_order_notional_usdt:D=D("0")
    portfolio_gross_after_entry_usdt:D=D("0")
    observed_equity_usdt:D=D("0")
    portfolio_effective_gross_multiple:D=D("0")
    selected_exchange_cross_leverage:int|None=None
    required_initial_margin_usdt:D=D("0")
    remaining_margin_after_order_and_fees_usdt:D=D("0")
    nominal_initial_margin_fits:bool=False
    frozen_strategy_notional_unchanged:bool=False
    account_observations_verified:bool=False
    liquidation_or_stop_survival_certified:bool=False
    live_change_allowed:bool=False
    live_authorization:bool=False
    mexc_posts:int=0
    database_writes:int=0


def _amount(v:object,*,positive:bool)->bool:
    return (
        type(v) is D and v.is_finite()
        and (v>0 if positive else v>=0)
    )


def assess_exact_v40_cross_leverage(witness:V40CrossLeverageInputs)->V40CrossLeveragePlan:
    """Select the minimum nominal leverage; NEVER modify the V40 order size.

    The initial margin for a new SHORT is approximated as N / L. Existing
    positions have ALREADY consumed initial margin, so their margin MUST NOT
    be deducted a second time from signed currently *available* margin.
    For new orders, the planned fee reserve and required remaining headroom
    are additionally subtracted from available collateral.
    """
    if type(witness) is not V40CrossLeverageInputs:
        raise V40LeveragePlanHalted("V40_LEVERAGE_WITNESS_TYPE_INVALID")
    w=witness
    if (
        type(w.signal_id) is not int or w.signal_id<=0
        or type(w.symbol) is not str
        or re.fullmatch(r"[A-Z0-9]+_USDT",w.symbol) is None
        or w.symbol in ("BTC_USDT","ETH_USDT")
        or any(not _amount(v,positive=True) for v in (
            w.frozen_lot_notional_usdt,
            w.executable_intent_notional_usdt,
            w.account_equity_usdt,
            w.signed_available_initial_margin_usdt,
        ))
        or any(not _amount(v,positive=False) for v in (
            w.current_gross_notional_usdt,
            w.independent_fee_reserve_usdt,
            w.required_remaining_margin_headroom_usdt,
        ))
        or any(
            type(v) is not int or not 1<=v<=125
            for v in (
                w.observed_contract_max_cross_leverage,
                w.observed_account_max_cross_leverage,
                w.operator_max_cross_leverage,
            )
        )
    ):
        raise V40LeveragePlanHalted("V40_LEVERAGE_SOURCE_OR_LIMITS_INVALID")
    if (
        w.account_cross_mode_verified is not True
        or w.account_identity_bookended_verified is not True
        or w.available_margin_private_signed_get_verified is not True
        or w.per_contract_and_private_leverage_tiers_verified is not True
        or w.frozen_strategy_lot_and_signal_verified is not True
    ):
        raise V40LeveragePlanHalted("V40_LEVERAGE_SIGNED_EXCHANGE_OR_LOT_PROOF_MISSING")
    if w.frozen_lot_notional_usdt!=w.executable_intent_notional_usdt:
        raise V40LeveragePlanHalted("V40_LEVERAGE_CANNOT_RESIZE_HISTORICAL_LOT")
    gross=w.current_gross_notional_usdt+w.frozen_lot_notional_usdt
    ceiling=min(
        MAX_REVIEW_LEVERAGE,
        w.observed_contract_max_cross_leverage,
        w.observed_account_max_cross_leverage,
        w.operator_max_cross_leverage,
    )
    available=w.signed_available_initial_margin_usdt
    reserve=w.independent_fee_reserve_usdt
    headroom=w.required_remaining_margin_headroom_usdt
    for lev in range(1,ceiling+1):
        needed=w.frozen_lot_notional_usdt/D(lev)
        remaining=available-needed-reserve
        if remaining>=headroom:
            return V40CrossLeveragePlan(
                status=STATUS_FEASIBLE,
                signal_id=w.signal_id,symbol=w.symbol,
                exact_order_notional_usdt=w.frozen_lot_notional_usdt,
                portfolio_gross_after_entry_usdt=gross,
                observed_equity_usdt=w.account_equity_usdt,
                portfolio_effective_gross_multiple=gross/w.account_equity_usdt,
                selected_exchange_cross_leverage=lev,
                required_initial_margin_usdt=needed,
                remaining_margin_after_order_and_fees_usdt=remaining,
                nominal_initial_margin_fits=True,
                frozen_strategy_notional_unchanged=True,
                account_observations_verified=True,
            )
    return V40CrossLeveragePlan(
        status="V40_EXACT_LOT_NO_SUPPORTED_CROSS_LEVERAGE_MARGIN_HALT",
        signal_id=w.signal_id,symbol=w.symbol,
        exact_order_notional_usdt=w.frozen_lot_notional_usdt,
        portfolio_gross_after_entry_usdt=gross,
        observed_equity_usdt=w.account_equity_usdt,
        portfolio_effective_gross_multiple=gross/w.account_equity_usdt,
        account_observations_verified=True,
        frozen_strategy_notional_unchanged=True,
    )
