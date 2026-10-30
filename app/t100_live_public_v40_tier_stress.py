"""Pure PUBLIC MEXC BY_VOLUME tier research at frozen T100 SL75 + 5% gap.

Returns only a redacted, NONAUTHORIZING scenario. A fixed contract quantity
stays in the same PUBLISHED BY_VOLUME tier during a common fair-price shock,
but its quote notional, fees and maintenance reserve increase. BY_VALUE and
unknown tiers are rejected. The worst PUBLISHED MMR is a current research
assumption only, never a verified ceiling on the venue's FUTURE risk levels.

No exchange client, credentials, private GET, DB, POST, order or gateway.
No module can upgrade risk-tier, collateral, liquidation or live permits.
"""
from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
import re
from typing import Any

from app.t100_live_incremental_tier_candidate import (
    classify_incremental_eligibility, reconstruct_incremental_candidate,
)

D = Decimal
_SL75 = D("1.75")
_GAP = D("0.05")


@dataclass(frozen=True)
class PublicV40TierScenario:
    status: str
    risk_limit_mode: str = "NOT_CHECKED"
    risk_limit_type: str = "NOT_CHECKED"
    public_contract_eligible: bool = False
    published_tiers_valid: bool = False
    published_tier_count: int | None = None
    candidate_in_published_tier: bool = False
    public_research_maintenance_rate: D | None = None
    assumed_gap_above_entry_pct: D = D("5")
    stop_reference_notional_multiple: D = _SL75
    stop_plus_gap_notional_multiple: D = _SL75 + _GAP
    entry_reference_notional_usdt: D | None = None
    research_stop_gap_notional_usdt: D | None = None
    research_published_maintenance_reserve_usdt: D | None = None
    research_published_liquidation_fee_reserve_usdt: D | None = None
    public_tiers_are_future_upper_bound: bool = False
    private_current_cross_verified: bool = False
    account_specific_api_order_access_verified: bool = False
    future_stressed_tiers_certified: bool = False
    account_collateral_certified: bool = False
    native_stop_protection_verified: bool = False
    venue_liquidation_certified: bool = False
    entry_allowed: bool = False
    live_authorization: bool = False
    mexc_orders_sent: bool = False
    read_only: bool = True


def _positive(value: Any) -> D:
    if value is None or type(value) is bool:
        raise ValueError("NUMERIC_UNVERIFIED")
    try:
        number = D(str(value))
    except (ValueError, TypeError, InvalidOperation):
        raise ValueError("NUMERIC_UNVERIFIED") from None
    if not number.is_finite() or number <= 0:
        raise ValueError("NUMERIC_UNVERIFIED")
    return number


def _ratio(value: Any) -> D:
    n = _positive(value)
    if n >= 1:
        raise ValueError("RATE_INVALID")
    return n


def assess_public_v40_tier_stress(
    contract: Any, *, contracts: Any, entry_reference_price: Any,
    leverage: int = 1,
) -> PublicV40TierScenario:
    def block(reason: str, **kw: Any) -> PublicV40TierScenario:
        return PublicV40TierScenario(status=reason, **kw)

    if type(contract) is not dict:
        return block("PUBLIC_V40_CONTRACT_UNVERIFIED")
    mode = contract.get("riskLimitMode")
    kind = contract.get("riskLimitType")
    mode = mode if type(mode) is str and mode in ("INCREASE", "CUSTOM") else "UNKNOWN"
    kind = kind if type(kind) is str and kind in ("BY_VOLUME", "BY_VALUE") else "UNKNOWN"
    common = dict(risk_limit_mode=mode, risk_limit_type=kind)
    symbol = contract.get("symbol")
    if (type(symbol) is not str
        or re.fullmatch(r"[A-Z0-9]+_USDT", symbol) is None
        or symbol in {"BTC_USDT", "ETH_USDT"}):
        return block("PUBLIC_V40_SYMBOL_UNVERIFIED", **common)
    eligible = classify_incremental_eligibility(contract).contract_eligible
    common["public_contract_eligible"] = eligible
    if not eligible:
        return block("PUBLIC_V40_CONTRACT_INELIGIBLE", **common)
    if kind != "BY_VOLUME" or mode == "UNKNOWN":
        return block("PUBLIC_V40_UNSUPPORTED_RISK_MODE", **common)
    if type(leverage) is not int or not 1 <= leverage <= 2:
        return block("PUBLIC_V40_LEVERAGE_OUTSIDE_RESEARCH_SCOPE", **common)
    try:
        qty = _positive(contracts)
        price = _positive(entry_reference_price)
        size = _positive(contract.get("contractSize"))
        liq_fee = _positive(contract.get("liquidationFeeRate"))
        if liq_fee >= 1:
            raise ValueError("LIQ_FEE_INVALID")
        maxlev = contract.get("maxLeverage")
        minlev = contract.get("minLeverage")
        regionlev = contract.get("countryConfigContractMaxLeverage", 0)
        if (type(maxlev) is not int or type(minlev) is not int
            or not 1 <= minlev <= leverage <= maxlev
            or type(regionlev) is not int or regionlev < 0
            or (regionlev > 0 and leverage > regionlev)):
            raise ValueError("LEVERAGE_UNVERIFIED")
        tiers: list[tuple[D, D, D, int]] = []
        if mode == "INCREASE":
            result = reconstruct_incremental_candidate(
                contract, position_contracts=qty, private_short_risk=None,
            )
            if result.status != "INCR_CANDIDATE_RECONSTRUCTED_NONAUTHORIZING":
                return block("PUBLIC_V40_INCREMENTAL_SCHEMA_OR_QUANTITY_BLOCKED",
                             **common)
            tiers = [(t.max_contracts, t.mmr, t.imr, maxlev) for t in result.tiers]
        else:
            raw = contract.get("riskLimitCustom")
            if type(raw) is not list or not 1 <= len(raw) <= 100:
                raise ValueError("CUSTOM_TIERS_UNVERIFIED")
            last_cap = D(0)
            last_mmr = D(0)
            last_imr = D(0)
            last_lev = 1000000
            for i, row in enumerate(raw, 1):
                if type(row) is not dict or type(row.get("level")) is not int or row["level"] != i:
                    raise ValueError("CUSTOM_LEVELS_INVALID")
                cap, mmr, imr = (
                    _positive(row.get("maxVol")),
                    _ratio(row.get("mmr")),
                    _ratio(row.get("imr")),
                )
                caplev = row.get("maxLeverage")
                if (type(caplev) is not int or caplev <= 0
                    or cap <= last_cap or mmr < last_mmr or imr < last_imr
                    or imr < mmr or caplev > last_lev):
                    raise ValueError("CUSTOM_TIERS_INCOHERENT")
                tiers.append((cap, mmr, imr, caplev))
                last_cap, last_mmr, last_imr, last_lev = cap, mmr, imr, caplev
        selected = next((x for x in tiers if qty <= x[0]), None)
        if selected is None:
            return block("PUBLIC_V40_QUANTITY_EXCEEDS_PUBLISHED_TIERS", **common)
        if leverage > selected[3]:
            return block("PUBLIC_V40_LEVERAGE_EXCEEDS_PUBLISHED_TIER", **common)
        worst_mmr = max(x[1] for x in tiers)
        if worst_mmr <= 0 or worst_mmr >= 1:
            raise ValueError("INVALID_MMR")
        entry_notional = qty * size * price
        stressed_notional = entry_notional * (_SL75 + _GAP)
        return PublicV40TierScenario(
            status="PUBLIC_V40_VOLUME_TIER_SCENARIO_NONAUTHORIZING",
            published_tiers_valid=True,
            published_tier_count=len(tiers),
            candidate_in_published_tier=True,
            public_research_maintenance_rate=worst_mmr,
            entry_reference_notional_usdt=entry_notional,
            research_stop_gap_notional_usdt=stressed_notional,
            research_published_maintenance_reserve_usdt=stressed_notional * worst_mmr,
            research_published_liquidation_fee_reserve_usdt=stressed_notional * liq_fee,
            **common,
        )
    except (ValueError, TypeError, ArithmeticError, InvalidOperation):
        return block("PUBLIC_V40_RISK_SCHEDULE_UNVERIFIED", **common)
