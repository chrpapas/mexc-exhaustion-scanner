"""Fail-closed symbol/account execution eligibility checks.

Consumes already-read public contract metadata plus the signed account
position mode. No exchange mutation and no live authorization.
"""
from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import Any

from app.mexc_trade import ContractSpec


class SymbolEligibilityHalted(RuntimeError):
    pass


@dataclass(frozen=True)
class SymbolExecutionEligibility:
    symbol: str
    api_allowed: bool
    hedge_mode_verified: bool
    contract_spec_sane: bool
    eligible: bool
    live_authorization: bool = False


def assess_symbol_execution_eligibility(
    *,
    symbol: str,
    contract_spec: ContractSpec,
    position_mode: Any,
) -> SymbolExecutionEligibility:
    if (
        not isinstance(symbol, str)
        or not symbol
        or not isinstance(contract_spec, ContractSpec)
        or contract_spec.symbol != symbol
    ):
        raise SymbolEligibilityHalted("SYMBOL_CONTRACT_IDENTITY_INVALID")

    try:
        size = Decimal(str(contract_spec.contract_size))
        step = Decimal(str(contract_spec.vol_unit))
        minimum = Decimal(str(contract_spec.min_vol))
        maximum = Decimal(str(contract_spec.max_vol))
    except Exception as exc:
        raise SymbolEligibilityHalted("CONTRACT_SPEC_NUMERIC_INVALID") from exc

    sane = all(
        x.is_finite() and x > 0
        for x in (size, step, minimum, maximum)
    ) and maximum >= minimum
    api_ok = contract_spec.api_allowed is True
    hedge_ok = (
        not isinstance(position_mode, bool)
        and str(position_mode) == "1"
    )
    eligible = sane and api_ok and hedge_ok

    return SymbolExecutionEligibility(
        symbol=symbol,
        api_allowed=api_ok,
        hedge_mode_verified=hedge_ok,
        contract_spec_sane=sane,
        eligible=eligible,
    )
