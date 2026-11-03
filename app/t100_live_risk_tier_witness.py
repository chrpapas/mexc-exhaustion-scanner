"""Strict, pure comparison of MEXC CURRENT account risk tier and public contract.

This is a risk-tier *witness*, NOT a certificate for future tiers. Neither
stressed_tier_upper_bound_verified nor live authorization is ever asserted.
No API calls, database, MEXC POSTs, leverage edits, or order actions.

MEXC primary specs:
* GET /api/v1/private/account/risk_limit (current short side, level, maxVol,
  maxLeverage, mmr, openType, leverage)
* GET /api/v1/contract/detail/country (public CUSTOM/BY_VOLUME riskLimitCustom,
  positionOpenType, state, apiAllowed, liquidationFeeRate)

The CURRENT private maxVol may convert a value limit using the current fair
price. Therefore this witness only models BY_VOLUME / CUSTOM static quantity
tiers, and still refuses future-dynamic-tier certification.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from typing import Any

D = Decimal


def _decimal(raw: Any, *, positive: bool = True) -> D:
    if type(raw) is bool or raw is None:
        raise ValueError("INVALID_NUMERIC")
    try:
        d = D(str(raw))
    except (TypeError, ValueError, InvalidOperation):
        raise ValueError("INVALID_NUMERIC") from None
    if not d.is_finite() or (d <= 0 if positive else d < 0):
        raise ValueError("INVALID_NUMERIC")
    return d


def _integer(raw: Any, *, minimum: int = 1) -> int:
    if type(raw) is not int or raw < minimum:
        raise ValueError("INVALID_INTEGER")
    return raw


@dataclass(frozen=True)
class TierWitness:
    status: str
    symbol: str = ""
    position_contracts: D = D(0)
    contract_size: D = D(0)
    current_private_level: int = 0
    modeled_volume_tier: int = 0
    conservative_schedule_mmr: D = D(0)
    liquidation_fee_rate: D = D(0)
    configured_leverage: int = 0
    current_risk_row_observed: bool = False
    public_static_tier_schedule_observed: bool = False
    stressed_tier_upper_bound_verified: bool = False
    live_authorization: bool = False
    entry_allowed: bool = False


def assess_risk_tier_witness(
    *, symbol: str, contracts: Any, private_risk: Any,
    public_contract: Any,
) -> TierWitness:
    """Fail closed on missing, cross-mode mismatch or incomplete tier schedules.

    MMR output is max(all CURRENT published custom tiers), not a forecast or
    verified bound on future exchange changes. Do NOT feed the output into
    the live risk gate as a fully certified tier just because it is numerical.
    """
    def block(reason: str) -> TierWitness:
        return TierWitness(status=reason)

    try:
        if (
            type(symbol) is not str
            or re.fullmatch(r"[A-Z0-9]+_USDT", symbol) is None
            or symbol in ("BTC_USDT", "ETH_USDT")
        ):
            return block("RISK_TIER_SYMBOL_INVALID")
        ncontracts = _decimal(contracts)
        if (
            not isinstance(private_risk, dict)
            or set(private_risk) != {symbol}
            or not isinstance(public_contract, dict)
            or public_contract.get("symbol") != symbol
        ):
            return block("RISK_TIER_SYMBOL_RESPONSE_MISMATCH")
        side_rows = private_risk[symbol]
        if not isinstance(side_rows, list) or not side_rows:
            return block("RISK_TIER_PRIVATE_ROWS_MISSING")
        shorts = [r for r in side_rows if type(r) is dict
                  and r.get("positionType") == 2 and type(r.get("positionType")) is int]
        if len(shorts) != 1 or any(type(r) is not dict for r in side_rows):
            return block("RISK_TIER_PRIVATE_SHORT_NOT_UNIQUE")
        if any(r.get("symbol") != symbol for r in side_rows):
            return block("RISK_TIER_PRIVATE_SYMBOL_DRIFT")
        short = shorts[0]
        _integer(short.get("level"))
        _integer(short.get("maxLeverage"))
        leverage = _integer(short.get("leverage"))
        if (
            short.get("openType") != 2
            or type(short.get("openType")) is not int
            or leverage > short["maxLeverage"]
            or ncontracts > _decimal(short.get("maxVol"))
        ):
            return block("RISK_TIER_PRIVATE_CROSS_OR_CAP_UNVERIFIED")
        private_mmr = _decimal(short.get("mmr"))
        if private_mmr >= 1:
            return block("RISK_TIER_PRIVATE_MMR_INVALID")
        if "currentMmr" in short:
            current_mmr = _decimal(short["currentMmr"])
            if current_mmr >= 1:
                return block("RISK_TIER_CURRENT_MMR_INVALID")
        else:
            current_mmr = private_mmr
        if (
            public_contract.get("riskLimitMode") != "CUSTOM"
            or public_contract.get("riskLimitType") != "BY_VOLUME"
        ):
            return block("RISK_TIER_STATIC_VOLUME_SCHEDULE_UNAVAILABLE")
        if (
            type(public_contract.get("positionOpenType")) is not int
            or public_contract["positionOpenType"] not in (2, 3)
            or type(public_contract.get("state")) is not int
            or public_contract["state"] != 0
            or public_contract.get("apiAllowed") is not True
            or public_contract.get("futureType") != 1
            or public_contract.get("settleCoin") != "USDT"
            or public_contract.get("quoteCoin") != "USDT"
        ):
            return block("RISK_TIER_PUBLIC_CONTRACT_NOT_ELIGIBLE")
        size = _decimal(public_contract.get("contractSize"))
        liquidation = _decimal(public_contract.get("liquidationFeeRate"), positive=False)
        if liquidation >= 1:
            return block("RISK_TIER_LIQUIDATION_FEE_INVALID")
        minimum_leverage = _integer(public_contract.get("minLeverage"))
        maximum_leverage = _integer(public_contract.get("maxLeverage"))
        country_leverage = public_contract.get("countryConfigContractMaxLeverage", 0)
        country_leverage = _integer(country_leverage, minimum=0)
        if not minimum_leverage <= leverage <= maximum_leverage or (
            country_leverage > 0 and leverage > country_leverage
        ):
            return block("RISK_TIER_PUBLIC_LEVERAGE_LIMIT")
        tiers = public_contract.get("riskLimitCustom")
        if not isinstance(tiers, list) or not tiers:
            return block("RISK_TIER_CUSTOM_LEVELS_MISSING")
        last_contracts = D(0)
        last_mmr = D(0)
        last_imr = D(0)
        last_leverage = 10**9
        levels = []
        for i, r in enumerate(tiers, 1):
            if type(r) is not dict or _integer(r.get("level")) != i:
                return block("RISK_TIER_LEVEL_SEQUENCE_INVALID")
            cap = _decimal(r.get("maxVol"))
            mmr = _decimal(r.get("mmr"))
            imr = _decimal(r.get("imr"))
            maxlev = _integer(r.get("maxLeverage"))
            if (
                cap <= last_contracts
                or mmr >= 1 or imr >= 1
                or mmr < last_mmr or imr < last_imr
                or imr < mmr
                or maxlev > last_leverage
            ):
                return block("RISK_TIER_LEVEL_MONOTONICITY_INVALID")
            levels.append((i, cap, mmr, imr, maxlev))
            last_contracts, last_mmr, last_imr, last_leverage = (
                cap, mmr, imr, maxlev
            )
        if short["level"] > len(levels):
            return block("RISK_TIER_PRIVATE_LEVEL_NOT_IN_SCHEDULE")
        private_tier = levels[short["level"] - 1]
        # Current private MMR may be higher due controls not in the public
        # base schedule; lower rates are not accepted as an invitation to
        # loosen the modeled reserve.
        if private_mmr < private_tier[2] or current_mmr < private_tier[2]:
            return block("RISK_TIER_PRIVATE_PUBLIC_MMR_CONFLICT")
        modeled = next((x for x in levels if ncontracts <= x[1]), None)
        if modeled is None:
            return block("RISK_TIER_POSITION_EXCEEDS_PUBLISHED_TIERS")
        if leverage > modeled[4]:
            return block("RISK_TIER_POSITION_LEVERAGE_EXCEEDS_TIER")
        conservative_mmr = max(last_mmr, private_mmr, current_mmr)
        return TierWitness(
            status="CURRENT_CUSTOM_VOLUME_RISK_SCHEDULE_OBSERVED_NONAUTHORIZING",
            symbol=symbol, position_contracts=ncontracts,
            contract_size=size,
            current_private_level=short["level"],
            modeled_volume_tier=modeled[0],
            conservative_schedule_mmr=conservative_mmr,
            liquidation_fee_rate=liquidation,
            configured_leverage=leverage,
            current_risk_row_observed=True,
            public_static_tier_schedule_observed=True,
        )
    except (ValueError, TypeError, KeyError, IndexError, ArithmeticError):
        return block("RISK_TIER_INPUT_INVALID")
