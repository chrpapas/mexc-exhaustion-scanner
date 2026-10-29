"""Offline candidate reconstruction of MEXC INCREASE/BY_VOLUME risk tiers.

MEXC documents incremental contract thresholds and maintenance/initial margin
increments, but this module is RESEARCH ONLY: API metadata may differ from
current account-specific limits and does NOT specify the full venue liquidation
engine. No POST/credentials/HTTP, no future-stressed tier certification or
order-authorization path. Rate/threshold values do not enter CLI output.
"""
from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from typing import Any


@dataclass(frozen=True)
class IncrementalTier:
    level: int
    max_contracts: Decimal
    mmr: Decimal
    imr: Decimal


@dataclass(frozen=True)
class IncrementalTierCandidate:
    status: str
    tiers: tuple[IncrementalTier, ...] = ()
    selected_position_tier: int = 0
    private_current_comparison: str = "NOT_CHECKED"
    short_specific_limits_used: bool = False
    public_contract_eligible: bool = False
    private_cross_short_verified: bool = False
    future_stressed_tiers_certified: bool = False
    venue_liquidation_certified: bool = False
    entry_allowed: bool = False
    live_authorization: bool = False


def _d(raw: Any, *, positive: bool = False) -> Decimal:
    if raw is None or type(raw) is bool:
        raise ValueError("INVALID_NUMBER")
    try:
        value = Decimal(str(raw))
    except (InvalidOperation, ValueError, TypeError):
        raise ValueError("INVALID_NUMBER") from None
    if not value.is_finite() or (value <= 0 if positive else value < 0):
        raise ValueError("INVALID_NUMBER")
    return value


def reconstruct_incremental_candidate(
    public_contract: Any, *, position_contracts: Any,
    private_short_risk: Any = None,
) -> IncrementalTierCandidate:
    """Pure candidate; compare reported private MMR without promoting it.

    Private level is NOT inferred solely from position volume: MEXC says
    it can be min(leverage-risk-level, position-risk-level).
    """
    def block(status: str) -> IncrementalTierCandidate:
        return IncrementalTierCandidate(status=status)

    if type(public_contract) is not dict:
        return block("INCR_PUBLIC_SCHEMA_UNVERIFIED")
    if (
        public_contract.get("riskLimitMode") != "INCREASE"
        or public_contract.get("riskLimitType") != "BY_VOLUME"
    ):
        return block("INCR_MODE_OR_TYPE_UNSUPPORTED")
    if (
        public_contract.get("state") != 0
        or public_contract.get("positionOpenType") not in (2, 3)
        or public_contract.get("apiAllowed") is not True
        or public_contract.get("futureType") != 1
        or public_contract.get("quoteCoin") != "USDT"
        or public_contract.get("settleCoin") != "USDT"
    ):
        return block("INCR_CONTRACT_NOT_ELIGIBLE")
    levels = public_contract.get("riskLevelLimit")
    if type(levels) is not int or not 1 <= levels <= 100:
        return block("INCR_LEVEL_COUNT_UNVERIFIED")
    side_switch = public_contract.get("riskLongShortSwitch")
    if type(side_switch) is not int or side_switch not in (0, 1):
        return block("INCR_SIDE_MODE_UNVERIFIED")
    try:
        contracts = _d(position_contracts, positive=True)
        _d(public_contract.get("contractSize"), positive=True)
        _d(public_contract.get("liquidationFeeRate"))
        base_mmr = _d(public_contract.get("maintenanceMarginRate"))
        base_imr = _d(public_contract.get("initialMarginRate"))
        mmr_step = _d(public_contract.get("riskIncrMmr"))
        imr_step = _d(public_contract.get("riskIncrImr"))
        if side_switch:
            base = _d(public_contract.get("riskBaseVolShort"), positive=True)
            step = _d(public_contract.get("riskIncrVolShort"))
        else:
            base = _d(public_contract.get("riskBaseVol"), positive=True)
            step = _d(public_contract.get("riskIncrVol"))
    except ValueError:
        return block(
            "INCR_SHORT_SIDE_FIELDS_MISSING"
            if side_switch == 1 else "INCR_NUMERIC_FIELDS_UNVERIFIED"
        )
    if (levels > 1 and step <= 0) or base_mmr >= 1 or base_imr >= 1 or base_imr < base_mmr:
        return block("INCR_PUBLIC_SCHEDULE_INCOHERENT")
    if _d(public_contract.get("liquidationFeeRate")) >= 1:
        return block("INCR_PUBLIC_SCHEDULE_INCOHERENT")
    tiers: list[IncrementalTier] = []
    for i in range(levels):
        limit = base + step * i
        mmr = base_mmr + mmr_step * i
        imr = base_imr + imr_step * i
        if (
            limit <= 0 or mmr >= 1 or imr >= 1 or mmr > imr
            or (tiers and limit <= tiers[-1].max_contracts)
        ):
            return block("INCR_PUBLIC_SCHEDULE_INCOHERENT")
        tiers.append(IncrementalTier(i + 1, limit, mmr, imr))

    modeled = next((tier.level for tier in tiers if contracts <= tier.max_contracts), 0)
    if modeled == 0:
        return block("INCR_POSITION_OUTSIDE_PUBLISHED_CANDIDATE")

    comparison = "PRIVATE_NOT_PROVIDED"
    private_ok = False
    if private_short_risk is not None:
        if (
            type(private_short_risk) is not dict
            or private_short_risk.get("positionType") != 2
            or private_short_risk.get("openType") != 2
        ):
            return block("INCR_PRIVATE_CROSS_SHORT_UNVERIFIED")
        level = private_short_risk.get("level")
        if type(level) is not int or level < 1 or level > levels:
            return block("INCR_PRIVATE_LEVEL_OUTSIDE_PUBLIC_SCHEDULE")
        try:
            actual_mmr = _d(private_short_risk.get("mmr"), positive=True)
            current_mmr = _d(
                private_short_risk.get("currentMmr", actual_mmr), positive=True
            )
            _d(private_short_risk.get("maxVol"), positive=True)
            leverage = private_short_risk.get("leverage")
            max_leverage = private_short_risk.get("maxLeverage")
            if (
                type(leverage) is not int or type(max_leverage) is not int
                or not 1 <= leverage <= max_leverage
            ):
                raise ValueError("INVALID_LEVERAGE")
        except ValueError:
            return block("INCR_PRIVATE_FIELDS_UNVERIFIED")
        candidate_mmr = tiers[level - 1].mmr
        if actual_mmr == candidate_mmr and current_mmr == candidate_mmr:
            comparison = "PRIVATE_CURRENT_MMR_EXACT_MATCH"
        elif actual_mmr >= candidate_mmr and current_mmr >= candidate_mmr:
            comparison = "PRIVATE_CURRENT_MMR_ABOVE_CANDIDATE"
        else:
            comparison = "PRIVATE_CURRENT_MMR_BELOW_CANDIDATE"
        private_ok = True

    return IncrementalTierCandidate(
        status="INCR_CANDIDATE_RECONSTRUCTED_NONAUTHORIZING",
        tiers=tuple(tiers), selected_position_tier=modeled,
        private_current_comparison=comparison,
        short_specific_limits_used=side_switch == 1,
        public_contract_eligible=True,
        private_cross_short_verified=private_ok,
    )
