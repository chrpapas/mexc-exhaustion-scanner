"""Pure, offline T100/V40 PUBLIC position-capacity research.

Converts hypothetical USDT notional to MEXC contracts using published
contractSize and volUnit (ROUND_DOWN). Separates maxVol single-order limit
from the BY_VOLUME published risk-tier position quantity limit.

Only PUBLISHED metadata and a caller-supplied reference price are considered.
Does not inspect private account risk limits or authenticate orders. A
public-only result never verifies private cross-margin mode, order permission,
market depth, liquidation, real price, live authorization, or account collateral.
It cannot split orders, increase notional to a minimum or submit an order.
"""
from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal, InvalidOperation, ROUND_DOWN
from typing import Any

from app.t100_live_incremental_tier_candidate import (
    reconstruct_incremental_candidate,
)
from app.t100_live_public_v40_tier_stress import (
    assess_public_v40_tier_stress,
)

D = Decimal


@dataclass(frozen=True)
class PublicV40CapacityReview:
    status: str
    desired_notional_usdt: D | None = None
    required_lots_rounded_down: D | None = None
    modeled_lots_notional_usdt: D | None = None
    public_max_single_order_lots: D | None = None
    public_max_position_tier_lots: D | None = None
    public_max_position_notional_usdt: D | None = None
    modeled_notional_fraction: D | None = None
    fits_public_single_order_limit: bool = False
    fits_public_position_tier: bool = False
    modeled_stop_gap_notional_usdt: D | None = None
    modeled_published_maintenance_reserve_usdt: D | None = None
    private_current_cap_verified: bool = False
    private_current_cross_verified: bool = False
    account_specific_order_access_verified: bool = False
    depth_and_execution_capacity_verified: bool = False
    liquidation_before_stop_certified: bool = False
    future_tiers_certified: bool = False
    account_collateral_certified: bool = False
    native_stop_protection_verified: bool = False
    entry_allowed: bool = False
    live_authorization: bool = False
    mexc_orders_sent: bool = False
    read_only: bool = True


def _pos(raw: Any) -> D:
    if raw is None or type(raw) is bool:
        raise ValueError("INVALID_NUMBER")
    try:
        n = D(str(raw))
    except (TypeError, InvalidOperation, ValueError):
        raise ValueError("INVALID_NUMBER") from None
    if not n.is_finite() or n <= 0:
        raise ValueError("INVALID_NUMBER")
    return n


def assess_public_v40_capacity(
    contract: Any, *, desired_notional_usdt: Any,
    entry_reference_price: Any, leverage: int = 1,
) -> PublicV40CapacityReview:
    def blocked(status: str, **fields: Any) -> PublicV40CapacityReview:
        return PublicV40CapacityReview(status=status, **fields)

    if type(contract) is not dict:
        return blocked("V40_CAPACITY_PUBLIC_METADATA_UNVERIFIED")
    try:
        target = _pos(desired_notional_usdt)
        price = _pos(entry_reference_price)
        size = _pos(contract.get("contractSize"))
        unit = _pos(contract.get("volUnit"))
        minimum = _pos(contract.get("minVol"))
        maxorder = _pos(contract.get("maxVol"))
        if minimum > maxorder or minimum % unit != 0 or maxorder % unit != 0:
            return blocked("V40_CAPACITY_VOLUME_GRID_UNVERIFIED")
        # Full venue/region eligibility and published schedule validation,
        # independently of the potentially oversized requested quantity.
        min_scenario = assess_public_v40_tier_stress(
            contract, contracts=minimum, entry_reference_price=price,
            leverage=leverage,
        )
        if min_scenario.status != "PUBLIC_V40_VOLUME_TIER_SCENARIO_NONAUTHORIZING":
            return blocked("V40_CAPACITY_PUBLIC_SCHEDULE_OR_ELIGIBILITY_BLOCKED")
        mode = contract.get("riskLimitMode")
        if mode == "INCREASE":
            levels = reconstruct_incremental_candidate(
                contract, position_contracts=minimum,
            )
            if levels.status != "INCR_CANDIDATE_RECONSTRUCTED_NONAUTHORIZING":
                return blocked("V40_CAPACITY_PUBLISHED_LEVELS_UNVERIFIED")
            maximum = levels.tiers[-1].max_contracts
        elif mode == "CUSTOM":
            rows = contract.get("riskLimitCustom")
            if not isinstance(rows, list) or not rows:
                return blocked("V40_CAPACITY_PUBLISHED_LEVELS_UNVERIFIED")
            maximum = _pos(rows[-1].get("maxVol"))
        else:
            return blocked("V40_CAPACITY_PUBLISHED_LEVELS_UNVERIFIED")
        if maximum < minimum or maximum % unit != 0:
            return blocked("V40_CAPACITY_PUBLISHED_LEVELS_UNVERIFIED")

        desired_lots = target / (price * size)
        # NEVER round up a requested position or claim unfilled residue is filled.
        lots = (
            (desired_lots / unit).to_integral_value(rounding=ROUND_DOWN) * unit
        )
        if lots < minimum:
            return blocked(
                "V40_CAPACITY_TARGET_BELOW_MINIMUM_NONAUTHORIZING",
                desired_notional_usdt=target,
                public_max_single_order_lots=maxorder,
                public_max_position_tier_lots=maximum,
                public_max_position_notional_usdt=maximum * size * price,
            )
        actual = lots * size * price
        common = dict(
            desired_notional_usdt=target,
            required_lots_rounded_down=lots,
            modeled_lots_notional_usdt=actual,
            public_max_single_order_lots=maxorder,
            public_max_position_tier_lots=maximum,
            public_max_position_notional_usdt=maximum * size * price,
            modeled_notional_fraction=actual / target,
            fits_public_single_order_limit=lots <= maxorder,
            fits_public_position_tier=lots <= maximum,
        )
        if lots > maximum:
            return blocked(
                "V40_CAPACITY_PUBLISHED_POSITION_TIER_EXCEEDED_NONAUTHORIZING",
                **common,
            )
        if lots > maxorder:
            return blocked(
                "V40_CAPACITY_PUBLISHED_SINGLE_ORDER_LIMIT_EXCEEDED_NONAUTHORIZING",
                **common,
            )
        scenario = assess_public_v40_tier_stress(
            contract, contracts=lots,
            entry_reference_price=price, leverage=leverage,
        )
        if scenario.status != "PUBLIC_V40_VOLUME_TIER_SCENARIO_NONAUTHORIZING":
            return blocked("V40_CAPACITY_SCENARIO_UNVERIFIED", **common)
        return blocked(
            "V40_CAPACITY_PUBLISHED_SINGLE_ORDER_FITS_NONAUTHORIZING",
            modeled_stop_gap_notional_usdt=
                scenario.research_stop_gap_notional_usdt,
            modeled_published_maintenance_reserve_usdt=
                scenario.research_published_maintenance_reserve_usdt,
            **common,
        )
    except (TypeError, ValueError, ArithmeticError, InvalidOperation):
        return blocked("V40_CAPACITY_INVALID_OR_INCOMPLETE")
