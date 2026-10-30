"""Pure V40 capacity *parity* policy over independently supplied evidence.

V40Admission.modeled_notional_usdt is ALREADY bounded by frozen VOL30, shock
budget and H4 turnover model; desired_notional_usdt is a diagnostic pre-cap
target, not the quantity that must be executed. Do NOT replace either.

A public capacity shortfall cannot be silently converted into another V40
sized trade. A deliberately downsized supervised pilot is a distinct purpose
handled by t100_live_pilot_sizing, never historical V40 trade parity.

This is an OFFLINE reasoning layer. It neither authenticates published
metadata nor verifies private MEXC risk limits, venue fills, book depth, stop
protection, collateral, account ownership, liquidation or live authorization.
No venue, DB, order, Render, HTTP, credentials or network calls.
"""
from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from typing import Any

from app.t100_v40_admission import V40Admission
from app.t100_live_public_v40_capacity import PublicV40CapacityReview

D = Decimal


@dataclass(frozen=True)
class V40PublicCapacityParity:
    status: str
    v40_research_modeled_notional_usdt: D | None = None
    quoted_public_capacity_usdt: D | None = None
    modeled_lots_notional_usdt: D | None = None
    modeled_notional_shortfall_usdt: D | None = None
    published_position_tier_fits: bool = False
    published_single_order_fits: bool = False
    published_full_modeled_notional_fits: bool = False
    historical_v40_quantity_parity_certified: bool = False
    private_account_risk_verified: bool = False
    private_cross_margin_verified: bool = False
    liquidity_and_stop_exit_verified: bool = False
    live_order_submission_allowed: bool = False
    live_authorization: bool = False
    mexc_orders_sent: bool = False
    read_only: bool = True


def _positive(raw: Any) -> D:
    if type(raw) is bool or raw is None:
        raise ValueError("INVALID_AMOUNT")
    try:
        result = D(str(raw))
    except (InvalidOperation, ValueError, TypeError):
        raise ValueError("INVALID_AMOUNT") from None
    if not result.is_finite() or result <= 0:
        raise ValueError("INVALID_AMOUNT")
    return result


def assess_v40_public_capacity_parity(
    admission: Any,
    public_capacity: Any,
) -> V40PublicCapacityParity:
    def blocked(status: str, **kw: Any) -> V40PublicCapacityParity:
        return V40PublicCapacityParity(status=status, **kw)

    if type(admission) is not V40Admission or admission.action != "ADMIT":
        return blocked("V40_CAPACITY_PARITY_NOT_AN_ADMITTED_MODEL")
    if type(public_capacity) is not PublicV40CapacityReview:
        return blocked("V40_CAPACITY_PARITY_PUBLIC_EVIDENCE_MISSING")
    try:
        modeled = _positive(admission.modeled_notional_usdt)
        desired = _positive(admission.desired_notional_usdt)
        target = _positive(public_capacity.desired_notional_usdt)
        if modeled > desired:
            return blocked("V40_CAPACITY_PARITY_FROZEN_MODEL_INCOHERENT")
        # Contract-size research must be performed for EXACTLY the modeled
        # V40 amount, not the gross tier target or a smaller pilot request.
        if target != modeled:
            return blocked("V40_CAPACITY_PARITY_TARGET_MISMATCH")
        public_cap = _positive(public_capacity.public_max_position_notional_usdt)
        qty = _positive(public_capacity.required_lots_rounded_down)
        notional = _positive(public_capacity.modeled_lots_notional_usdt)
        _positive(public_capacity.public_max_single_order_lots)
        _positive(public_capacity.public_max_position_tier_lots)
        if notional > modeled:
            return blocked("V40_CAPACITY_PARITY_QUANTITY_ROUNDED_UP")
    except ValueError:
        return blocked("V40_CAPACITY_PARITY_NUMERIC_EVIDENCE_INCOMPLETE")
    result = dict(
        v40_research_modeled_notional_usdt=modeled,
        quoted_public_capacity_usdt=public_cap,
        modeled_lots_notional_usdt=notional,
        modeled_notional_shortfall_usdt=modeled - notional,
        published_position_tier_fits=public_capacity.fits_public_position_tier,
        published_single_order_fits=public_capacity.fits_public_single_order_limit,
    )

    if (
        public_capacity.status
        == "V40_CAPACITY_PUBLISHED_POSITION_TIER_EXCEEDED_NONAUTHORIZING"
        or not public_capacity.fits_public_position_tier
    ):
        return blocked("V40_CAPACITY_PARITY_PUBLISHED_POSITION_CAP_REJECT",
                       **result)
    if (
        public_capacity.status
        == "V40_CAPACITY_PUBLISHED_SINGLE_ORDER_LIMIT_EXCEEDED_NONAUTHORIZING"
        or not public_capacity.fits_public_single_order_limit
    ):
        return blocked("V40_CAPACITY_PARITY_SINGLE_ORDER_CAP_REJECT",
                       **result)
    if (
        public_capacity.status
        != "V40_CAPACITY_PUBLISHED_SINGLE_ORDER_FITS_NONAUTHORIZING"
    ):
        return blocked("V40_CAPACITY_PARITY_PUBLIC_OUTCOME_UNVERIFIED",
                       **result)

    # True is deliberately *public-only* feasibility, NEVER live or
    # historical performance parity, and never a license to split orders.
    return blocked(
        "V40_CAPACITY_PARITY_PUBLIC_QUANTITY_FITS_NONAUTHORIZING",
        published_full_modeled_notional_fits=True, **result,
    )
